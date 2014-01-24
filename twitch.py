#!python3

import threading
import json
from urllib import parse, request

TWITCH_API_CHANNELS = 'https://api.twitch.tv/kraken/channels/%s'
TWITCH_API_CHANNELS_FOLLOWS = TWITCH_API_CHANNELS + '/follows'
TWITCH_API_STREAMS = 'https://api.twitch.tv/kraken/streams/%s'

class TwitchAPIRequest(threading.Thread):
    def __init__(self, url, method='GET', data=None, oauth_token=None, *args, **kwargs):
        if data and method == 'GET':
            url += '?' + parse.urlencode(data)
        self.request = request.Request(url, method=method)
        self.request.add_header('Accept', 'application/vnd.twitchtv.v3+json')
        if oauth_token:
            self.request.add_header('Authorization', 'OAuth ' + oauth_token)
        if data and method != 'GET':
            self.request.data = parse.urlencode(data).encode('utf-8')
        self.result = None
        self.error = None
        super().__init__(*args, target=self.process, **kwargs)
        self.start()

    def run(self, *args, **kwargs):
        try:
            super().run(*args, **kwargs)
        except Exception as e:
            self.error = e

    def process(self):
        result = json.loads(request.urlopen(self.request).read().decode('utf-8'))
        if not result:
            self.error = 'unknown error'
        elif 'error' in result:
            self.error = result['error']
        else:
            self.result = result

if __name__ == '__main__':
    channel = 'kutu182'
    req = TwitchAPIRequest(TWITCH_API_STREAMS % channel)
    # req = TwitchAPIRequest(TWITCH_API_CHANNELS_FOLLOWS % channel, data=dict(limit=1))
    # req = TwitchAPIRequest(TWITCH_API_CHANNELS % channel, 'PUT', { 'channel[status]': 'test' })
    req.join(5)
    print(json.dumps(req.result, indent=2))
    print(req.error)
