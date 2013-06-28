import logging
import hashlib
import time

from django import forms
from django.core.cache import cache
from django.utils.translation import ugettext_lazy as _
from vidscraper import auto_search
from vidscraper.utils.search import intersperse_results

from localtv.models import Video
from localtv.settings import API_KEYS

from vidscraper.exceptions import VidscraperError

class LiveSearchForm(forms.Form):
    LATEST = 'latest'
    RELEVANT = 'relevant'
    ORDER_BY_CHOICES = (
        (LATEST, _('Latest')),
        (RELEVANT, _('Relevant')),
    )
    query = forms.CharField()
    order_by = forms.ChoiceField(choices=ORDER_BY_CHOICES, initial=LATEST,
                                 required=False)

    def clean_order_by(self):
        return self.cleaned_data.get('order_by') or self.LATEST

    def _get_cache_key(self):
        return 'localtv-livesearch-%s' % (
            hashlib.md5('%(query)s-%(order_by)s' % self.cleaned_data
                        ).hexdigest())

    def get_results(self):
        cache_key = self._get_cache_key()
        results = cache.get(cache_key)
        if results is None:
            finish_by = time.time() + 20
            search_results = auto_search(self.cleaned_data['query'],
                                  order_by=self.cleaned_data['order_by'],
                                  api_keys=API_KEYS)
            results = []
            for vidscraper_video in intersperse_results(search_results, 40):
                try:
                    vidscraper_video.load()
                except VidscraperError:
                    pass
                except Exception:
                    logging.error('error while loading search result: %r',
                                  vidscraper_video.url,
                                  exc_info=True)
                else:
                    results.append(vidscraper_video)
                if time.time() > finish_by:
                    break # don't take forever!
            cache.set(cache_key, results)

        for vidscraper_video in results:
            video = Video.from_vidscraper_video(vidscraper_video, commit=False)
            if video.embed_code or video.file_url:
                yield video
