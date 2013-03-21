# Miro Community - Easiest way to make a video website
#
# Copyright (C) 2009, 2010, 2011, 2012 Participatory Culture Foundation
#
# Miro Community is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# Miro Community is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Miro Community.  If not, see <http://www.gnu.org/licenses/>.

import datetime

from django.conf import settings
from django.contrib.sites.models import Site
from django.views.generic import ListView
from haystack.query import SearchQuerySet

from localtv.search.utils import NormalizedVideoList
from localtv.search.views import SortFilterView
from localtv.search_indexes import DATETIME_NULL_PLACEHOLDER


MAX_VOTES_PER_CATEGORY = getattr(settings, 'MAX_VOTES_PER_CATEGORY', 3)


class CompatibleListingView(SortFilterView):
    """
    This is the backwards-compatible version of the :class:`SortFilterView`,
    which provides some extra context, normalizes the search results as
    :class:`.Video` instances, and provides some querystring handling.

    """
    #: Period of time within which the video was approved.
    approved_since = None

    def get_paginate_by(self, queryset):
        paginate_by = self.request.GET.get('count')
        if paginate_by:
            try:
                paginate_by = int(paginate_by)
            except ValueError:
                paginate_by = None
        if paginate_by is None:
            paginate_by = self.paginate_by
        return paginate_by

    def get_form_data(self, base_data=None, filter_value=None):
        data = super(CompatibleListingView, self).get_form_data(base_data,
                                                                filter_value)
        if 'q' not in data:
            data['q'] = data.get('query', '')
        if data.get('sort') == 'latest':
            data['sort'] = 'newest'
        return data

    def get_queryset(self):
        """Wraps the normal queryset in a :class:`.NormalizedVideoList`."""
        qs = super(CompatibleListingView, self).get_queryset()

        if self.approved_since is not None:
            if isinstance(qs, SearchQuerySet):
                qs = qs.exclude(when_approved__exact=DATETIME_NULL_PLACEHOLDER)
            else:
                qs = qs.exclude(when_approved__isnull=True)
            qs = qs.filter(when_approved__gt=(
                                datetime.datetime.now() - self.approved_since))

        return NormalizedVideoList(qs)

    def get_context_data(self, **kwargs):
        context = super(CompatibleListingView, self).get_context_data(
                                                                     **kwargs)
        form = context['form']
        context['query'] = form['q'].value()
        context['video_list'] = context['videos']
        return context


class SiteListView(ListView):
    """
    Filters the ordinary queryset according to the current site.

    """
    def get_queryset(self):
        return super(SiteListView, self).get_queryset().filter(
                                site=Site.objects.get_current())
