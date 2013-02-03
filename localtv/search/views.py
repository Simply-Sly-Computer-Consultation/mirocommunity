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

from django.conf import settings
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.http import Http404, HttpResponseRedirect
from django.views.generic import ListView

from localtv.search.forms import SearchForm, ModelFilterField


VIDEOS_PER_PAGE = getattr(settings, 'VIDEOS_PER_PAGE', 15)


class SortFilterMixin(object):
    """
    This mixin defines a standard way of handling sorting and filtering on a
    class, with optional enforced sorts and filters.

    """
    #: If provided, the name of a filter which will be enforced and can't be
    #: overridden.
    filter_name = None

    #: If provided, the name of a sort which will be enforced and can't be
    #: overridden.
    sort = None

    form_class = SearchForm

    def get_form_data(self, base_data=None, filter_value=None):
        data = base_data or {}
        if self.filter_name is not None:
            data[self.filter_name] = filter_value
        if self.sort is not None:
            data['sort'] = self.sort
        return data

    def get_form(self, base_data=None, filter_value=None):
        return self.form_class(self.get_form_data(base_data, filter_value))


class SortFilterView(ListView, SortFilterMixin):
    """
    Generic view for videos; implements pagination, filtering and searching.

    """
    paginate_by = VIDEOS_PER_PAGE
    form_class = SearchForm
    context_object_name = 'videos'

    #: The kwarg expected from the urlpattern for this view if
    #: :attr:`filter_name` is not ``None``. Default: 'pk'.
    filter_kwarg = 'pk'

    def get_queryset(self):
        """
        Returns the results of :attr:`form_class`\ 's ``get_queryset()``
        method.

        """
        if self.filter_name is None:
            filter_value = None
        else:
            field = self.form_class.base_fields[self.filter_name]
            filter_value = self.kwargs.get(self.filter_kwarg)
            # ModelFilterFields expect a list.
            if isinstance(field, ModelFilterField):
                filter_value = [filter_value]
        form = self.form = self.get_form(self.request.GET.dict(),
                                         filter_value)
        return form.search()

    def get_object(self):
        if self.filter_name is not None:
            field = self.form_class.base_fields[self.filter_name]
            if isinstance(field, ModelFilterField):
                model = field.model
                try:
                    key = '{0}__iexact'.format(field.to_field_name or 'pk')
                    kwargs = {key: self.kwargs[self.filter_kwarg]}
                    obj = model.objects.get(**kwargs)
                except (ValueError, model.DoesNotExist):
                    raise Http404
                else:
                    return obj
        return None

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if hasattr(self.object, 'get_absolute_url'):
            # User doesn't have a useful get_absolute_url.
            if isinstance(self.object, User):
                absolute_url = reverse('localtv_author',
                                       args=(self.object.pk,))
            else:
                absolute_url = self.object.get_absolute_url()
            if absolute_url != request.path:
                return HttpResponseRedirect(absolute_url)
        return super(SortFilterView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(SortFilterView, self).get_context_data(**kwargs)
        context['form'] = self.form
        if (self.filter_name is not None and
            isinstance(self.form_class.base_fields.get(self.filter_name),
                       ModelFilterField)):
            # If there is a model instance that's being filtered on, put it
            # into the context.
            context[self.filter_name] = self.object
        return context
