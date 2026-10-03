"""Fail-closed private delivery. No credentials or destination IDs in source."""
import os
import requests

class Slack:
    def __init__(self,token):
        self.session=requests.Session()
        self.session.headers['Authorization']='Bearer '+token
    def call(self,method,payload):
        r=self.session.post('https://slack.com/api/'+method,json=payload,timeout=(10,30))
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
    if allowed_user not in members or members-set([allowed_user,actor]):
        raise ValueError('unexpected_recipient')

def deliver(text,client_message_id):
    # Called only by an explicitly reviewed report workflow. No automatic
    # inference publishes from the lexical candidate queue in this version.
    if os.getenv('SLACK_PUBLISH_ENABLED','false')!='true':
        raise ValueError('publishing_disabled')
    token=os.getenv('SLACK_BOT_TOKEN','')
    if not token:
        raise ValueError('slack_credential_missing')
    client=Slack(token)
    channel=os.getenv('SLACK_DESTINATION_CHANNEL','')
    validate_destination(client,channel,os.getenv('SLACK_ALLOWED_USER_ID',''))
    return client.call('chat.postMessage',{'channel':channel,'text':text,
        'client_msg_id':client_message_id,'unfurl_links':False,'unfurl_media':False})
