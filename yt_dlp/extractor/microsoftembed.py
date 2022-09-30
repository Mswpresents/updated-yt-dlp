from .common import InfoExtractor
from ..utils import (
    traverse_obj,
    unified_timestamp,
    int_or_none,
)

class MicrosoftEmbedIE(InfoExtractor):
    _VALID_URL = r'https?://(?:www\.)?microsoft\.com/en-us/videoplayer/embed/(?P<id>[a-z0-9A-Z]+)'
    _TESTS = [{
        'url': 'https://www.microsoft.com/en-us/videoplayer/embed/RWL07e',
        'info_dict': {
            'id': 'RWL07e',
            'title': '...'
        }
    }]
    _API_URL = 'https://prod-video-cms-rt-microsoft-com.akamaized.net/vhs/api/videos/'

    def _real_extract(self, url):
        video_id = self._match_id(url)

        metadata = self._download_json(
            self._API_URL + video_id,
            video_id)

        formats = []
        for source_type, source in metadata['streams'].items():
            stream_url = source['url']

            if source_type == 'smoothStreaming':
                formats.extend(self._extract_ism_formats(stream_url, video_id, 'mss'))
            elif source_type in ('apple_HTTP_Live_Streaming', 'mPEG_DASH'):
                formats.extend(self._extract_m3u8_formats(stream_url, video_id))
            else: 
                formats.append({
                    'format_id': source_type,
                    'url': stream_url
                    # height...
                    #width ...
                })
            # else:
            #     formats[key] = value
        # TODO:
        # - extract subtitles (see expected return format in common.py)
        # - extract thumbnails (see expected return format in common.py)
        output = {
            'id': video_id,
            'title': traverse_obj('snippet', 'title'),
            #'thumbnails': metadata['snippet']['thumbnails'],  # will need to extract into thumbnail list ofrmat
            'timestamp': metadata['snippet']['activeStartDate'],  # use unified_timestamp from utils
            'formats': formats,
            'age_limit': int_or_none(traverse_obj('snippet', 'minimumAge')) or 0
        }
        return output