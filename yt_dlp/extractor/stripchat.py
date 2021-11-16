# coding: utf-8
from __future__ import unicode_literals

from .common import InfoExtractor
from ..compat import (
    compat_str,
)
from ..utils import (
    ExtractorError,
    lowercase_escape,
    try_get,
)


class StripchatIE(InfoExtractor):
    _VALID_URL = r'https?://stripchat\.com/(?P<id>[0-9A-Za-z-_]+)'
    _TEST = {
        'url': 'https://stripchat.com/feel_me',
        'info_dict': {
            'id': 'feel_me',
            'ext': 'mp4',
            'title': 're:^feel_me [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}$',
            'description': str,
            'is_live': True,
        },
        'skip': 'Room is offline',
    }

    _ROOM_OFFLINE = 'Model is offline.'
    _ROOM_PRIVATE = 'Model is in private show.'

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(
            'https://stripchat.com/%s/' % video_id, video_id,
            headers=self.geo_verification_headers())

        data = self._parse_json(
            self._search_regex(
                r'<script\b[^>]*>\s*window\.__PRELOADED_STATE__\s*=(?P<value>.*?)<\/script>',
                webpage, 'data', default='{}', group='value'),
            video_id, transform_source=lowercase_escape, fatal=False)
        if not data:
            raise ExtractorError('Unable to find configuration for stream.')

        if try_get(data, lambda x: x['viewCam']['show'], dict):
            raise ExtractorError('Model is in private show', expected=True)
        elif not try_get(data, lambda x: x['viewCam']['model']['isLive'], bool):
            raise ExtractorError('Model is offline', expected=True)

        server = try_get(data, lambda x: x['viewCam']['viewServers']['flashphoner-hls'], compat_str)
        host = try_get(data, lambda x: x['config']['data']['hlsStreamHost'], compat_str)
        model_id = try_get(data, lambda x: x['viewCam']['model']['id'], int)

        formats = self._extract_m3u8_formats(
            'https://b-%s.%s/hls/%d/%d.m3u8' % (server, host, model_id, model_id),
            video_id, ext='mp4', m3u8_id='hls', fatal=False, live=True)
        self._sort_formats(formats)

        return {
            'id': video_id,
            'title': self._live_title(video_id),
            'description': self._og_search_description(webpage),
            'is_live': True,
            'formats': formats,
        }
