# pytest automatically imports email_cape_client fixture in conftest.py
import requests
import time
from cape.client import CapeClient
from cape_email_plugin.email_settings import MAILGUN_API_KEY
from cape_email_plugin.tests.tests_settings import TEST_SEND_EMAIL_DOMAIN, TEST_RECEIVE_EMAIL_DOMAIN

from uuid import uuid4


MAILGUN_WAIT_PERIOD = 30
MAILGUN_RETRIES = 5


def _mailgun_get_last_email(to_email: str):
    """Returns the last email sent to the given email address, in the format:
        {'Received': 'by luna.mailgun.net with HTTP; Fri, 16 Feb 2018 16:26:24 +0000',
         'stripped-signature': '', 'content-id-map': {},
         'Sender': 'bla=bla.com@thecape.ai', 'recipients': 'bla@thecape.ai',
         'subject': 'testing', 'To': 'bla@thecape.ai',
         'message-headers': [['Content-Transfer-Encoding', '7bit'], ['Received', 'by luna.mailgun.net with HTTP; Fri, 16 Feb 2018 16:26:24 +0000'], ['Date', 'Fri, 16 Feb 2018 16:26:24 +0000'], ['Sender', 'bla=bla.com@thecape.ai'], ['Message-Id', '<20180216162624.1.EDF932EC6946397C@thecape.ai>'], ['To', 'bla@thecape.ai'], ['From', 'bla@bla.com'], ['Subject', 'testing'], ['Content-Type', 'text/html; charset="ascii"'], ['Mime-Version', '1.0']],
         'stripped-text': 'This is a test',
         'From': 'bla@bla.com', 'attachments': [],
         'from': 'bla@bla.com',
         'sender': 'postmaster@thecape.ai', 'Content-Transfer-Encoding': '7bit',
         'stripped-html': 'This is a test',
         'body-html': 'This is a test', 'Mime-Version': '1.0', 'Date': 'Fri, 16 Feb 2018 16:26:24 +0000',
         'Message-Id': '<20180216162624.1.EDF932EC6946397C@thecape.ai>', 'Content-Type': 'text/html; charset="ascii"',
         'body-plain': 'This is a test', 'Subject': 'testing'}
    """
    for i in range(MAILGUN_RETRIES):
        try:
            url = f"https://api.mailgun.net/v3/{to_email.split('@')[1]}/events?ascending=no&event=stored&limit=1&to={to_email.lower()}"  # because of mailgun bug
            response = requests.get(url, auth=("api", MAILGUN_API_KEY)).json()
            retrieval_url = response['items'][0]['storage']['url']
            return requests.get(retrieval_url, auth=("api", MAILGUN_API_KEY)).json()
        except IndexError as e:
            if i == MAILGUN_RETRIES - 1:
                raise e
            time.sleep(MAILGUN_WAIT_PERIOD)


def test_emails(email_cape_client: CapeClient):
    #
    # Testing Bob->Cape Error->Bob
    #
    user_token = email_cape_client.get_user_token()
    question = 'What colour is the sky ?'
    cape_email = f"{user_token}@{TEST_RECEIVE_EMAIL_DOMAIN}"
    bob_email = f"bob@{TEST_SEND_EMAIL_DOMAIN}"
    alice_email = f"alice@{TEST_SEND_EMAIL_DOMAIN}"
    answer = "The sky is blue."
    random_key = str(uuid4())
    bob_email_content = f"Hello {random_key},\n{question}\nRegards,\nBob"
    alice_answer = f"Hello {random_key},\n {answer} \nRegards,\nAlice"
    bob_email_subject = "Test sky colour"
    # First attempt we get an error for not setting forward email
    _mailgun_send(email_from=bob_email, email_to=cape_email, email_subject=bob_email_subject,
                  email_text=bob_email_content)
    time.sleep(MAILGUN_WAIT_PERIOD)
    sent_body_plain = _mailgun_get_last_email(cape_email)['body-plain']
    assert random_key in sent_body_plain
    assert question in sent_body_plain
    error_response = _mailgun_get_last_email(bob_email)['body-plain']
    assert 'Sorry, this Cape AI account has not yet been configured for email access. \nPlease contact your Cape administrator to set this up.' in error_response
    assert email_cape_client.get_profile()['forwardEmail'] is None
    assert email_cape_client.set_forward_email(alice_email)
    assert email_cape_client.get_profile()['forwardEmail'] == alice_email
    assert email_cape_client.get_profile()['forwardEmailVerified'] == False
    time.sleep(MAILGUN_WAIT_PERIOD)
    verification_token_text = _mailgun_get_last_email(alice_email)['body-plain']
    verification_token = verification_token_text.split("verifiedEmailToken=")[1].split("\n")[0]
    email_cape_client._raw_api_call('user/verify-forward-email', {"verifiedEmailToken":verification_token})
    assert email_cape_client.get_profile()['forwardEmailVerified'] == True
    time.sleep(60)  # we wait for the forwardEmailVerified attribute to be propagated across nodes
    #
    # Testing Bob->Cape Suggestions->Alice->Cape New Saved Reply->Bob
    #
    _mailgun_send(email_from=bob_email, email_to=cape_email, email_subject=bob_email_subject,
                  email_text=bob_email_content)
    time.sleep(MAILGUN_WAIT_PERIOD)
    sent_body_plain = _mailgun_get_last_email(cape_email)['body-plain']
    reply = _mailgun_get_last_email(alice_email)
    suggestions: str = reply['body-plain']
    assert random_key in sent_body_plain
    assert question in sent_body_plain
    assert random_key in suggestions
    assert """==Cape AI Suggestions==""" in suggestions
    # mailgun's plain-text version adds line breaks when encountering our <b> tags around the answer
    suggestions = suggestions.replace("\n", "")
    assert suggestions.index("sky is blue") < suggestions.index("colour is red") < suggestions.index("like pizzas")
    # Alice replies with the answer
    test_sender = reply['Sender'].replace('thecape.ai', TEST_RECEIVE_EMAIL_DOMAIN)
    _mailgun_send(email_from=alice_email, email_to=test_sender,
                  email_subject="Re: " + bob_email_subject,
                  email_text=alice_answer)
    time.sleep(MAILGUN_WAIT_PERIOD)
    alice_response = _mailgun_get_last_email(test_sender)['body-plain']
    assert answer in alice_response
    bob_response = _mailgun_get_last_email(bob_email)['body-plain']
    assert answer in bob_response
    assert random_key in bob_response
    #
    # Testing Bob->Cape Saved Reply->Bob
    #
    random_key = str(uuid4())
    bob_email_content = f"Hello {random_key},\n{question}\nRegards,\nBob"
    _mailgun_send(email_from=bob_email, email_to=cape_email, email_subject=bob_email_subject,
                  email_text=bob_email_content)
    time.sleep(MAILGUN_WAIT_PERIOD)
    sent_body_plain = _mailgun_get_last_email(cape_email)['body-plain']
    bob_response = _mailgun_get_last_email(bob_email)['body-plain']
    assert answer in bob_response
    assert random_key in sent_body_plain
    assert question in sent_body_plain
    assert random_key in bob_response


def _mailgun_send(email_from, email_to, email_subject, email_text):
    data = {'from': email_from, 'to': email_to, 'subject': email_subject, 'html': email_text}
    requests.post(f'https://api.mailgun.net/v3/{TEST_RECEIVE_EMAIL_DOMAIN}/messages', data=data,
                  auth=('api', f'{MAILGUN_API_KEY}'))
