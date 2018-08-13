import re
import json
from datetime import datetime
import html
import hashlib
import hmac
import quotequail
from functools import wraps

from webservices.app.app_settings import URL_BASE, UI_URL
from webservices.app.app_settings import app_email_endpoints
from logging import debug, warning
from responder.responder_settings import MAILGUN_API_KEY, MAILGUN_DOMAIN, DEFAULT_EMAIL

import requests
from webservices.app.app_middleware import respond_with_json
from api_helpers.exceptions import UserException
from api_helpers.input import required_parameter
from api_helpers.text_responses import *
from userdb.email_event import EmailEvent
from userdb.user import User
from webservices.app.app_core import _answer as responder_answer
from webservices.app.app_saved_reply_endpoints import _create_saved_reply as create_saved_reply

_endpoint_route = lambda x: app_email_endpoints.route(URL_BASE + x, methods=['GET', 'POST'])

_GREETINGS = {"hola", "hi", "dear", "hey", "hello", "morning", "afternoon", "evening","bonjour" }
_BYES = {"thank", "bye", "regards", "cheers", "sincerely", "ciao", "best", "bgif", "soon", "cordially", "yours", "sent",
         "--", "goodbye"}  # yours truly, sent from my iphone, talk soon, see you soon...

NON_WORD_CHARS = re.compile('[^0-9a-zA-Z\s]')

"""
    When sending email the following cases can occur, if Bob has a question for Alice:
       - User has not configured forward email, no saved reply is created:
         bob@gmail.com -A-> token@thecape.ai -G-> bob@gmail.com
       - AI responds correctly, no saved reply is created:
         bob@gmail.com -A-> token@thecape.ai -D-> bob@gmail.com
       - AI responds incorrectly, new saved reply is created:
         bob@gmail.com -A-> token@thecape.ai -D-> bob@gmail.com -E-> correctId@thecape.ai -F-> alice@gmail.com -C-> replyId@thecape.ai -D-> bob@gmail.com
       - AI suggests to Alice, new saved reply is created:
         bob@gmail.com -A-> token@thecape.ai -B-> alice@gmail.com -C-> replyId@thecape.ai -D-> bob@gmail.com
       - AI does not respond, new saved reply is created
         bob@gmail.com -A-> token@thecape.ai -B-> alice@gmail.com -C-> replyId@thecape.ai -D-> bob@gmail.com
    In case we did a correct match but the answer info is incorrect alice should repair it in the admin panel)
"""


def mailgun(wrapped):
    """
    Decorator for handling API calls that provide a metadata dictionary as input.
    Calls the wrapped function with the parsed metadata provided by the API user.
    """

    @wraps(wrapped)
    def decorated(request, *args, **kwargs):
        token = required_parameter(request, 'token')
        timestamp = required_parameter(request, 'timestamp')
        signature = required_parameter(request, 'signature')
        hmac_digest = hmac.new(key=MAILGUN_API_KEY.encode(), msg=(timestamp + token).encode(),
                               digestmod=hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, hmac_digest):
            raise UserException(MAILGUN_INVALID_SIGNATURE)

        local_part = request['args']['recipient'].split('@')[0]
        if local_part[-2] == '+':
            email_event: EmailEvent = EmailEvent.get('unique_id', local_part[:-2])
            if email_event is None:
                raise UserException(INVALID_TOKEN % local_part[:-2])
            user = User.get('user_id', email_event.user_id)
        else:
            user: User = User.get('token', local_part)
            email_event = None
        if user is not None:
            request['user_from_token'] = user
            request['user'] = user
        else:
            mailgun_send(request['args']['to'], request['args']['from'], request['args']['subject'],
                         ERROR_EMAIL_TOKEN_NOT_FOUND % local_part)
            return {"success": False, "emailHandled": True}

        if email_event is None:
            return wrapped(request, *args, **kwargs, user=user)
        else:
            return wrapped(request, *args, **kwargs, user=user, email_event=email_event)

    return decorated


def mailgun_send(email_from, email_to, email_subject, email_text):
    if email_to.lower().endswith(MAILGUN_DOMAIN):
        warning("Refusing to send email to %s (%s domain)" % (email_to, MAILGUN_DOMAIN))
    else:
        data = {'from': email_from, 'to': email_to, 'subject': email_subject, 'html': email_text}
        requests.post(f'https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages', data=data,
                      auth=('api', f'{MAILGUN_API_KEY}'))


def _mailgun_reply(email_from: str, email_to: str, email_original_subject: str, email_original_text: str,
                   email_original_timestamp: datetime, email_reply_text: str):
    email_text = email_reply_text
    email_text += email_original_timestamp.strftime(
        f"<br />On %a, %b %d, %Y at %H:%M %p {email_to} wrote:<br />")
    email_text += "<br />".join(f"> {line}" for line in email_original_text.replace('\r\n', '\n').split('\n'))
    mailgun_send(email_from, email_to, f'Re: {email_original_subject}', email_text)


def _simple_heuristic(line: str, lookup_set: set = _GREETINGS, first_words: int = 3) -> bool:
    line = re.sub(NON_WORD_CHARS, "", line)#remove punctuation
    words = line.split()[:first_words]  # Are the first X words in the lookup_set
    return bool(lookup_set.intersection(word.lower() for word in words))


def _get_body(text: str):  # TODO build a classifier for this
    unwrapped_text = quotequail.unwrap(text)
    # Remove quotes
    if unwrapped_text is None:
        stripped_text = text
    elif 'text_top' in unwrapped_text:
        stripped_text = unwrapped_text['text_top']
    elif 'text_bottom' in unwrapped_text:
        stripped_text = unwrapped_text['text_bottom']
    lines = [line.strip() for line in stripped_text.replace('\r\n', '\n').split('\n') if line.strip()]
    if len(lines) > 1 and _simple_heuristic(lines[0]):
        lines.pop(0)
    for idx in range(1, len(lines) - 1):
        if _simple_heuristic(lines[idx], _BYES):
            lines = lines[:idx]
            break
    return "\n".join(lines)


def _respond_with_answer(email_event: EmailEvent, answer: dict):
    """
    In this case we just received an email to token@thecape.ai and found a saved reply to answer with.
    """
    answer_text = answer["answerText"].replace("\n", "\r\n")
    email_original_text = email_event.question_email_package['body-plain']
    email_from = f'Cape AI <{email_event.unique_id}+d@{MAILGUN_DOMAIN}>'
    email_to = email_event.question_email_sender
    email_original_subject = email_event.question_email_package['subject']
    firstname = ' ' + email_event.question_email_package['from'].split()[0]
    if '@' in firstname:
        firstname = ''
    signature_text = '--<br />Sent by <a href="https://alpha.thecape.ai">Cape</a> AI<br /><br />'
    email_to_correct = html.escape(f'"Cape AI" <{email_event.unique_id}+e@{MAILGUN_DOMAIN}>')
    email_subject_correct = html.escape(email_original_subject)
    email_body_correct = html.escape(email_original_text)
    email_body_correct = email_body_correct.replace('\r\n', '%0D%0A')
    signature_text += f'<small>If this answer does not answer your question, click <a href="mailto:{email_to_correct}?subject={email_subject_correct}&body={email_body_correct}">here</a>.</small>'
    email_reply_text = f'Hello{firstname},<br /><br />{answer_text}<br /><br />{signature_text}<br /><br />'
    _mailgun_reply(email_from, email_to, email_original_subject, email_original_text,
                   email_event.question_email_timestamp, email_reply_text)
    email_event.final_email_saved_reply_id = answer['sourceId']
    email_event.final_email_sender = email_from
    email_event.final_email_timestamp = datetime.utcnow()
    email_event.save()


def _request_assistance(user: User, email_event: EmailEvent, answers: dict=None):
    """
    In this case we just received an email to token@thecape.ai and found several machine reading suggestions.
    """
    firstname = ' ' + email_event.question_email_package['from'].split()[0]
    if '@' in firstname:
        firstname = ''

    answer_text = f'Hello{firstname},<br /><br />==Cape AI Suggestions==<br /><br />'
    if answers == None:
        answer_text += ERROR_NO_SUGGESTIONS + "<br /><br />"
    else:
        for answer in answers:
            if 'answerContext' in answer:
                context = answer["answerContext"]
                local_start_offset = answer['answerTextStartOffset'] - answer['answerContextStartOffset']
                local_end_offset = answer['answerTextEndOffset'] - answer['answerContextStartOffset']
                context = f'{context[:local_start_offset]}<b>{context[local_start_offset:local_end_offset]}</b>{context[local_end_offset:]}'
            else:
                context = answer['answerText']
            if answer['sourceType'] == 'document':
                answer_text += f'{context}<br /><br />According to document: ' \
                               f'<a href="{UI_URL}/dashboard.html#/documents/{answer["sourceId"]}">{answer["sourceId"]}</a>' \
                               f'<br /><br />'
            elif answer['sourceType'] == 'saved_reply':
                answer_text += f'{context}<br /><br />According to ' \
                               f'<a href="{UI_URL}/dashboard.html#/saved-replies/{answer["sourceId"]}">saved reply</a>' \
                               f'<br /><br />'
    answer_text += f'==End Suggestions==<br /><br />'
    answer_text += '--<br/>Sent by <a href="https://alpha.thecape.ai">Cape</a> AI<br /><br />'
    email_from = f'Cape AI <{email_event.unique_id}+c@{MAILGUN_DOMAIN}>'  # the reply goes to 'c'
    email_to = user.verified_email
    email_original_subject = email_event.question_email_package['subject']
    email_original_text = email_event.question_email_package['body-plain']
    _mailgun_reply(email_from, email_to, email_original_subject, email_original_text,
                   email_event.question_email_timestamp, answer_text)
    email_event.suggested_email_results = answers
    email_event.suggested_email_sender = email_from
    email_event.suggested_email_timestamp = datetime.utcnow()
    email_event.save()


@_endpoint_route('/email/question')
@respond_with_json
@mailgun
def email_question(request, user: User):
    """In this case we received an email to token@thecape.ai"""
    extracted_body = _get_body(request['args']['body-plain'])
    email_event = EmailEvent(user_id=user.user_id, question_email_package=dict(request['args']),
                             question_email_extracted_body=extracted_body,
                             question_email_sender=request['args']['from'],
                             question_email_timestamp=datetime.utcfromtimestamp(int(request['args']['timestamp'])))
    email_event.save()
    if user.forward_email == DEFAULT_EMAIL:
        mailgun_send(request['args']['to'], request['args']['from'], request['args']['subject'],
                     ERROR_EMAIL_UNCONFIGURED)
        return {"success": False, "emailHandled": True}
    if user.verified_email is None:
        mailgun_send(request['args']['to'], request['args']['from'], request['args']['subject'],
                     ERROR_EMAIL_UNVALIDATED)
        return {"success": False, "emailHandled": True}

    request['args']['token'] = user.token
    request['args']['question'] = extracted_body
    request['args']['numberofitems'] = '3'
    response = json.loads(responder_answer(request).body)
    if response['success']:
        answers = response['result']['items']
        if len(answers) == 0:
            _request_assistance(user, email_event, None)
        elif answers[0]['sourceType'] == 'saved_reply':
            _respond_with_answer(email_event, answers[0])
        else:
            _request_assistance(user, email_event, answers)
    else:
        _request_assistance(user, email_event, None)

    return {"success": True, "emailHandled": True}


@_endpoint_route('/email/new-reply')
@respond_with_json
@mailgun
def email_new_reply(request, user: User, email_event: EmailEvent):
    """In this case Alice has corrected the email, we create a new saved reply and send Alice's mail."""
    answerText = _get_body(request['args']['body-plain'])
    request['args']['question'] = email_event.question_email_extracted_body
    request['args']['answer'] = answerText
    email_from = f'Cape AI <{email_event.unique_id}+c@{MAILGUN_DOMAIN}>'

    # Verify that this email came from the verified_email address
    match = re.search("(?:<(.*)>|^([^<].*[^> ])$)", request['args']['from'])
    if match.groups()[0]:
        sender = match.groups()[0]
    elif match.groups()[1]:
        sender = match.groups()[1]
    else:
        warning("Invalid email address: %s" % request['args']['from'])
        return {"success": False, "emailHandled": True}

    if sender != user.verified_email:
        mailgun_send(email_from, sender, request['args']['subject'], ERROR_UNRECOGNISED_SENDER % sender)
        return {"success": False, "emailHandled": True}

    try:
        reply_id = json.loads(create_saved_reply(request).body)['result']['replyId']
        answer = {
            'sourceId': reply_id,
            'answerText': answerText
        }
        _respond_with_answer(email_event, answer)
    except UserException as e:
        mailgun_send(email_from, user.verified_email, request['args']['subject'], e)

    return {"success": True, "emailHandled": True}


@_endpoint_route('/email/request-correction')
@respond_with_json
@mailgun
def email_request_correction(request, user: User, email_event: EmailEvent):
    """Bob didn't think the answer was correct, so we email Alice for a correction."""
    extracted_body = _get_body(request['args']['body-plain'])
    request['args']['token'] = user.token
    request['args']['question'] = extracted_body
    request['args']['numberofitems'] = '3'
    response = json.loads(responder_answer(request).body)
    if response['success']:
        answers = response['result']['items']
        _request_assistance(user, email_event, answers)
    else:
        _request_assistance(user, email_event, None)

    return {"success": True, "emailHandled": True}

