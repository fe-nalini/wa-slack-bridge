"""Fail-closed private delivery. No credentials or destination IDs in source."""
import os
import requests

class DeliveryUncertain(Exception):
    """Slack may have accepted the POST; an automatic resend is unsafe."""

class Slack:
    def __init__(self,token):
        self.session=requests.Session()
        self.session.headers['Authorization']='Bearer '+token
    def call(self,method,payload):
        self.last_method=method
        if method in {'auth.test','conversations.info','conversations.members','conversations.history'}:
            r=self.session.get('https://slack.com/api/'+method,params=payload,timeout=(10,30))
        elif method=='chat.postMessage':
            r=self.session.post('https://slack.com/api/'+method,json=payload,timeout=(10,30))
        else:
            raise ValueError('unsupported_slack_method')
        if not r.ok:
            raise ValueError('slack_http_'+str(r.status_code))
        d=r.json()
        if not d.get('ok'):
            raise ValueError('slack_'+str(d.get('error','unknown')))
        return d

def validate_destination(client,channel,allowed_user):
    if not channel or not allowed_user:
        raise ValueError('destination_not_configured')
    actor=client.call('auth.test',{})['user_id']
    expected_actor=os.getenv('SLACK_EXPECTED_BOT_USER_ID','')
    if expected_actor and actor!=expected_actor:
        raise ValueError('unexpected_app_identity')
    info=client.call('conversations.info',{'channel':channel})['channel']
    if not info.get('is_private') or info.get('is_shared') or info.get('is_ext_shared'):
        raise ValueError('destination_not_exclusive_private')
    members=set();cursor=''
    for _ in range(20):
        data=client.call('conversations.members',{'channel':channel,'limit':200,'cursor':cursor})
        members.update(data.get('members',[]))
        cursor=data.get('response_metadata',{}).get('next_cursor','')
        if not cursor:break
    else:raise ValueError('membership_not_fully_verified')
    if allowed_user not in members or actor not in members or members-set([allowed_user,actor]):
        raise ValueError('unexpected_recipient')

def probe_destination():
    """Read-only check using protected runtime configuration, even with sending off."""
    token=os.getenv('SLACK_BOT_TOKEN','')
    if not token:
        return {'status':'blocked','code':'slack_credential_missing','stage':'configuration'}
    client=Slack(token)
    stages={'auth.test':'authentication','conversations.info':'channel',
            'conversations.members':'membership'}
    try:
        validate_destination(client,os.getenv('SLACK_DESTINATION_CHANNEL',''),
                             os.getenv('SLACK_ALLOWED_USER_ID',''))
    except ValueError as exc:
        # Only fixed codes may reach logs; never echo response bodies or secrets.
        code=str(exc)
        allowed={'destination_not_configured','destination_not_exclusive_private',
                 'membership_not_fully_verified','unexpected_recipient','unexpected_app_identity',
                 'slack_invalid_auth','slack_not_authed','slack_token_revoked',
                 'slack_account_inactive','slack_missing_scope','slack_channel_not_found',
                 'slack_not_in_channel','slack_ratelimited','slack_http_429',
                 'slack_http_400','slack_http_401','slack_http_403','slack_http_404',
                 'slack_not_allowed_token_type','slack_access_denied','slack_team_access_not_granted',
                 'slack_org_login_required','slack_ekm_access_denied','slack_invalid_arguments',
                 'slack_method_deprecated','slack_not_allowed','slack_is_bot','slack_user_is_bot'}
        return {'status':'blocked','code':code if code in allowed else 'slack_check_failed',
                'stage':stages.get(getattr(client,'last_method',''),'configuration')}
    except Exception:
        return {'status':'unavailable','code':'slack_check_unavailable',
                'stage':stages.get(getattr(client,'last_method',''),'configuration')}
    return {'status':'ready','code':'verified_private_destination','stage':'complete'}

def deliver(text,client_message_id):
    # Called only by an explicitly reviewed report workflow. No automatic
    # inference publishes from the lexical candidate queue in this version.
    if os.getenv('SLACK_PUBLISH_ENABLED','false')!='true':
        raise ValueError('publishing_disabled')
    return _send(text,client_message_id)

def deliver_test(text,client_message_id):
    # Fixed technical message only; independent of disabled analysis publishing.
    from .diagnostics import TEST_TEXT
    if not os.getenv('SLACK_DELIVERY_TEST_ID') or text!=TEST_TEXT:
        raise ValueError('technical_test_not_authorized')
    return _send(text,client_message_id)

def _send(text,client_message_id):
    token=os.getenv('SLACK_BOT_TOKEN','')
    if not token:
        raise ValueError('slack_credential_missing')
    client=Slack(token)
    channel=os.getenv('SLACK_DESTINATION_CHANNEL','')
    validate_destination(client,channel,os.getenv('SLACK_ALLOWED_USER_ID',''))
    try:
        return client.call('chat.postMessage',{'channel':channel,'text':text,
            'client_msg_id':client_message_id,'unfurl_links':False,'unfurl_media':False})
    except (requests.RequestException, KeyError, TypeError) as exc:
        raise DeliveryUncertain('send_outcome_unknown') from exc
    except ValueError as exc:
        # HTTP 5xx or malformed JSON can follow acceptance. Explicit Slack
        # rejection or rate limit is safe to classify without exposing payloads.
        code=str(exc)
        if code.startswith('slack_') and not code.startswith('slack_http_5'):
            raise
        raise DeliveryUncertain('send_outcome_unknown') from exc
