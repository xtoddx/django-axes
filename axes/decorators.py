from datetime import datetime, timedelta
import logging

from django.conf import settings
from django.contrib.auth import logout
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render_to_response
from django.template import RequestContext

from axes.models import AccessAttempt
import axes

# see if the user has overridden the failure limit
FAILURE_LIMIT = getattr(settings, 'AXES_LOGIN_FAILURE_LIMIT', 3)

# see if the user has set axes to lock out logins after failure limit
LOCK_OUT_AT_FAILURE = getattr(settings, 'AXES_LOCK_OUT_AT_FAILURE', True)

USE_USER_AGENT = getattr(settings, 'AXES_USE_USER_AGENT', False)

COOLOFF_TIME = getattr(settings, 'AXES_COOLOFF_TIME', None)
if isinstance(COOLOFF_TIME, int):
    COOLOFF_TIME = timedelta(hours=COOLOFF_TIME)

LOGGER = getattr(settings, 'AXES_LOGGER', 'axes.watch_login')

LOCKOUT_TEMPLATE = getattr(settings, 'AXES_LOCKOUT_TEMPLATE', None)
LOCKOUT_URL = getattr(settings, 'AXES_LOCKOUT_URL', None)
VERBOSE = getattr(settings, 'AXES_VERBOSE', True)

def query2str(items):
    return '\n'.join(['%s=%s' % (k, v) for k,v in items])

log = logging.getLogger(LOGGER)
if VERBOSE:
    log.info('AXES: BEGIN LOG')
    log.info('Using django-axes ' + axes.get_version())

def get_user_attempt(request):
    """
    Returns access attempt record if it exists.
    Otherwise return None.
    """
    ip = request.META.get('REMOTE_ADDR', '')
    if USE_USER_AGENT:
        ua = request.META.get('HTTP_USER_AGENT', '<unknown>')
        attempts = AccessAttempt.objects.filter(
            user_agent=ua,
            ip_address=ip
        )
    else:
        attempts = AccessAttempt.objects.filter(
            ip_address=ip
        )

    if not attempts:
        return None

    attempt = attempts[0]
    if COOLOFF_TIME and attempt.attempt_time + COOLOFF_TIME < datetime.now():
        attempt.delete()
        return None

    return attempt

def watch_login(func):
    """
    Used to decorate the django.contrib.admin.site.login method.
    """

    def decorated_login(request, *args, **kwargs):
        # share some useful information
        if func.__name__ != 'decorated_login' and VERBOSE:
            log.info('AXES: Calling decorated function: %s' % func.__name__)
            if args: log.info('args: %s' % args)
            if kwargs: log.info('kwargs: %s' % kwargs)

        # call the login function
        response = func(request, *args, **kwargs)

        if func.__name__ == 'decorated_login':
            # if we're dealing with this function itself, don't bother checking
            # for invalid login attempts.  I suppose there's a bunch of
            # recursion going on here that used to cause one failed login
            # attempt to generate 10+ failed access attempt records (with 3
            # failed attempts each supposedly)
            return response

        if request.method == 'POST':
            # see if the login was successful
            login_unsuccessful = (
                response and
                not response.has_header('location') and
                response.status_code != 302
            )
            if check_request(request, login_unsuccessful):
                return response

            if LOCKOUT_TEMPLATE:
                context = RequestContext(request, {
                    'cooloff_time': COOLOFF_TIME,
                    'failure_limit': FAILURE_LIMIT,
                })
                return render_to_response(LOCKOUT_TEMPLATE, context)

            if LOCKOUT_URL:
                return HttpResponseRedirect(LOCKOUT_URL)

            if COOLOFF_TIME:
                return HttpResponse("Account locked: too many login attempts.  "
                                    "Please try again later.")
            else:
                return HttpResponse("Account locked: too many login attempts.  "
                                    "Contact an admin to unlock your account.")
        return response

    return decorated_login

def check_request(request, login_unsuccessful):
    failures = 0
    attempt = get_user_attempt(request)

    if attempt:
        failures = attempt.failures_since_start

    # no matter what, we want to lock them out
    # if they're past the number of attempts allowed
    if failures > FAILURE_LIMIT and LOCK_OUT_AT_FAILURE:
        # We log them out in case they actually managed to enter
        # the correct password.
        logout(request)
        log.warn('AXES: locked out %s after repeated login attempts.' %
                 attempt.ip_address)
        return False

    if login_unsuccessful:
        # add a failed attempt for this user
        failures += 1

        # Create an AccessAttempt record if the login wasn't successful
        # has already attempted, update the info
        if attempt:
            attempt.get_data = '%s\n---------\n%s' % (
                attempt.get_data,
                query2str(request.GET.items()),
            )
            attempt.post_data = '%s\n---------\n%s' % (
                attempt.post_data,
                query2str(request.POST.items())
            )
            attempt.http_accept = request.META.get('HTTP_ACCEPT', '<unknown>')
            attempt.path_info = request.META.get('PATH_INFO', '<unknown>')
            attempt.failures_since_start = failures
            attempt.attempt_time = datetime.now()
            attempt.save()
            log.info('AXES: Repeated login failure by %s. Updating access '
                     'record. Count = %s' %
                     (attempt.ip_address, failures))
        else:
            ip = request.META.get('REMOTE_ADDR', '')
            ua = request.META.get('HTTP_USER_AGENT', '<unknown>')
            attempt = AccessAttempt.objects.create(
                user_agent=ua,
                ip_address=ip,
                get_data=query2str(request.GET.items()),
                post_data=query2str(request.POST.items()),
                http_accept=request.META.get('HTTP_ACCEPT', '<unknown>'),
                path_info=request.META.get('PATH_INFO', '<unknown>'),
                failures_since_start=failures
            )
            log.info('AXES: New login failure by %s. Creating access record.' %
                     ip)
    else:
        # user logged in -- forget the failed attempts
        if attempt:
            attempt.delete()

    return True
