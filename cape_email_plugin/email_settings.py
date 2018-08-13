from sanic import Blueprint
import os

URL_BASE = '/email'

email_event_endpoints = Blueprint('email_event_endpoints')

MAILGUN_API_KEY = os.getenv('CAPE_MAILGUN_API_KEY', 'REPLACEME')
MAILGUN_DOMAIN = os.getenv('CAPE_MAILGUN_DOMAIN', 'REPLACEME')
DEFAULT_EMAIL = os.getenv('CAPE_DEFAULT_EMAIL', 'REPLACEME')
