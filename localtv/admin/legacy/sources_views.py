import re

from django.contrib.auth.models import User
from django.core.paginator import Paginator, InvalidPage
from django.core.urlresolvers import reverse_lazy
from django.db.models import Q
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render_to_response, get_object_or_404
from django.template.context import RequestContext
from django.utils.encoding import force_unicode
from django.views.decorators.csrf import csrf_protect
from django.views.generic import UpdateView, DeleteView

from localtv.decorators import require_site_admin
from localtv.models import SiteSettings, Feed, SavedSearch, Category, VIDEO_SERVICE_REGEXES
from localtv.utils import SortHeaders, MockQueryset
from localtv.admin.legacy import forms

VIDEO_SERVICE_TITLES = (
    re.compile(r'Uploads by (.+)'),
    re.compile(r"Vimeo / (.+)'s uploaded videos")
    )


## -------------------
## Source administration
## -------------------

@require_site_admin
@csrf_protect
def manage_sources(request):
    headers = SortHeaders(request, (
            ('Source', 'name__lower'),
            ('Categories', None),
            ('User Attribution', None),
            ('Type', 'type'),
            ('Import', None),
            ('Auto Approve', 'auto_approve')))

    sort = headers.order_by()
    if sort.endswith('type'):
        if sort[0] == '-':
            orm_sort = '-name__lower'
        else:
            orm_sort = 'name__lower'
    else:
        orm_sort = sort
    site_settings = SiteSettings.objects.get_current()
    feeds = Feed.objects.filter(
        site=site_settings.site).extra(select={
            'name__lower': 'LOWER(name)'}).order_by(orm_sort)
    searches = SavedSearch.objects.filter(
        site=site_settings.site).extra(select={
            'name__lower': 'LOWER(query_string)'}).order_by(
            orm_sort)

    search_string = request.GET.get('q', '')

    if search_string:
        feeds = feeds.filter(Q(feed_url__icontains=search_string) |
                             Q(name__icontains=search_string) |
                             Q(webpage__icontains=search_string) |
                             Q(description__icontains=search_string))
        searches = searches.filter(query_string__icontains=search_string)

    category = request.GET.get('category')
    if category:
        category = get_object_or_404(Category, pk=category)
        feeds = feeds.filter(auto_categories=category)
        searches = searches.filter(auto_categories=category)

    author = request.GET.get('author')
    if author:
        author = get_object_or_404(User, pk=author)
        feeds = feeds.filter(auto_authors=author)
        searches = searches.filter(auto_authors=author)

    source_filter = request.GET.get('filter')
    if source_filter == 'search':
        queryset = searches
    elif source_filter in ('feed', 'user'):
        q = Q(feed_url__iregex=VIDEO_SERVICE_REGEXES[0][1])
        for service, regexp in VIDEO_SERVICE_REGEXES[1:]:
            q = q | Q(feed_url__iregex=regexp)
        if source_filter == 'user':
            queryset = feeds.filter(q)
        else:
            queryset = feeds.exclude(q)
    else:
        reverse = False
        if orm_sort[0] == '-':
            reverse = True
            orm_sort = orm_sort[1:]
        feeds_list = [(force_unicode(getattr(feed, orm_sort)), feed)
                      for feed in feeds]
        searches_list = [(force_unicode(getattr(search, orm_sort)), search)
                         for search in searches]
        queryset = [l[1] for l in sorted(feeds_list + searches_list,
                                         reverse=reverse)]

    if sort.endswith('type'):
        reverse = (sort[0] == '-')
        queryset = sorted(queryset,
                          reverse=reverse,
                          key=lambda source: source.source_type().lower())
    paginator = Paginator(queryset, 15)
    try:
        page = paginator.page(int(request.GET.get('page', 1)))
    except InvalidPage:
        raise Http404

    if request.method == 'POST':
        formset = forms.SourceFormset(request.POST, request.FILES,
                                      queryset=MockQueryset(page.object_list))
        if formset.is_valid():
            formset.save()
            bulk_action = request.POST.get('bulk_action', '')
            for form in formset.bulk_forms:
                if bulk_action == 'remove':
                    if request.POST.get('keep'):
                        form.instance.video_set.all().update(
                            search=None, feed=None)
                    form.instance.delete()

            for form in formset.deleted_forms:
                if request.POST.get('keep'):
                    form.instance.video_set.all().update(search=None,
                                                         feed=None)
                form.instance.delete()

            path = request.get_full_path()
            if '?' in path:
                return HttpResponseRedirect(path + '&successful')
            else:
                return HttpResponseRedirect(path + '?successful')
    else:
        formset = forms.SourceFormset(queryset=MockQueryset(page.object_list))

    return render_to_response('localtv/admin/sources/manage.html',
                              {
            'add_feed_form': forms.AddFeedForm(),
            'page': page,
            'paginator': paginator,
            'headers': headers,
            'search_string': search_string,
            'source_filter': source_filter,
            'categories': formset._qs_cache['categories'],
            'users': formset._qs_cache['authors'],
            'successful': 'successful' in request.GET,
            'formset': formset},
                              context_instance=RequestContext(request))


class EditSearchView(UpdateView):
    model = SavedSearch
    form_class = forms.EditSearchForm
    context_object_name = 'search'
    template_name = 'localtv/admin/sources/search_edit.html'

    def get_success_url(self):
        return self.request.path


class DeleteSearchView(DeleteView):
    model = SavedSearch
    success_url = reverse_lazy('localtv_admin_manage_page')

    def get(self, *args, **kwargs):
        return self.delete(*args, **kwargs)


edit_search = require_site_admin(csrf_protect(EditSearchView.as_view()))
delete_search = require_site_admin(csrf_protect(DeleteSearchView.as_view()))
