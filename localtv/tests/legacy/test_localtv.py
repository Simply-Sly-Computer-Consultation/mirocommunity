import datetime
import os.path
import shutil
import tempfile
from urllib import quote_plus, urlencode

import feedparser
import vidscraper

from django.conf import settings
from django.contrib.auth.models import User, AnonymousUser
from django.contrib.comments import get_model, get_form, get_form_target
Comment = get_model()
CommentForm = get_form()

from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sites.models import Site
from django.core.files import storage
from django.core import mail
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.http import HttpRequest
from django.test import TestCase
from django.test.client import Client, RequestFactory

from haystack import connections
from haystack.query import SearchQuerySet
from requests.models import Response

import localtv
import localtv.templatetags.filters
from localtv.middleware import UserIsAdminMiddleware
from localtv import models
from localtv.models import Watch, Category, SiteSettings, Video, Feed
from localtv import utils
import localtv.feeds.views
from localtv.search.utils import NormalizedVideoList
from localtv.tasks import haystack_batch_update

from notification import models as notification


Profile = utils.get_profile_model()
TEST_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(localtv.__file__), 'tests', 'legacy', 'data'))
VIDSCRAPER_TEST_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(vidscraper.__file__), 'tests', 'data'))


class FakeRequestFactory(RequestFactory):
    """Constructs requests with any necessary attributes set."""
    def request(self, **request):
        request = super(FakeRequestFactory, self).request(**request)
        request.user = AnonymousUser()
        UserIsAdminMiddleware().process_request(request)
        SessionMiddleware().process_request(request)
        return request


class BaseTestCase(TestCase):
    fixtures = ['site', 'users']

    def run(self, *args, **kwargs):
        # hack to prevent the test runner from treating abstract classes as
        # something with tests to run
        if self.__class__.__dict__.get('abstract'):
            return
        else:
            return TestCase.run(self, *args, **kwargs)

    def setUp(self):
        TestCase.setUp(self)
        self.old_site_id = settings.SITE_ID
        settings.SITE_ID = 1
        SiteSettings.objects.clear_cache()
        self.site_settings = SiteSettings.objects.get_current()

        self.old_MEDIA_ROOT = settings.MEDIA_ROOT
        self.tmpdir = tempfile.mkdtemp()
        settings.MEDIA_ROOT = self.tmpdir
        Profile.__dict__['logo'].field.storage = \
            storage.FileSystemStorage(self.tmpdir)
        self.old_CACHES = settings.CACHES
        settings.CACHES = {
            'default':
                {'BACKEND':
                     'django.core.cache.backends.dummy.DummyCache'}}
        mail.outbox = [] # reset any email at the start of the suite
        self.factory = FakeRequestFactory()

    def _fixture_setup(self):
        index = connections['default'].get_unified_index().get_index(Video)
        index._teardown_save()
        index._teardown_delete()
        super(BaseTestCase, self)._fixture_setup()
        index._setup_save()
        index._setup_delete()
        self._rebuild_index()

    def _fixture_teardown(self):
        index = connections['default'].get_unified_index().get_index(Video)
        index._teardown_save()
        index._teardown_delete()
        super(BaseTestCase, self)._fixture_teardown()
        index._setup_save()
        index._setup_delete()
        self._clear_index()

    def tearDown(self):
        TestCase.tearDown(self)
        settings.SITE_ID = self.old_site_id
        settings.MEDIA_ROOT = self.old_MEDIA_ROOT
        settings.CACHES = self.old_CACHES
        Profile.__dict__['logo'].field.storage = \
            storage.default_storage
        shutil.rmtree(self.tmpdir)

    @staticmethod
    def get_response(content, code=200):
        response = Response()
        if hasattr(content, 'read'):
            content = content.read()
        response._content = content
        response.status_code = code
        return response

    @classmethod
    def _data_path(cls, test_path):
        """
        Given a path relative to localtv/tests/testdata, returns an absolute path.

        """
        return os.path.join(TEST_DATA_DIR, test_path)

    @classmethod
    def _data_file(cls, test_path, mode='r'):
        """
        Returns the absolute path to a file in our legacy testdata directory.
        """
        return open(cls._data_path(test_path), mode)

    @classmethod
    def _vidscraper_data_path(cls, test_path):
        """
        Given a path relative to vidscraper/tests/data, returns an absolute path.

        """
        return os.path.join(VIDSCRAPER_TEST_DATA_DIR, test_path)

    @classmethod
    def _vidscraper_data_file(cls, test_path, mode='r'):
        """
        Given a path relative to vidscraper/tests/data, returns an open file.

        """
        return open(cls._vidscraper_data_path(test_path), mode)

    def assertStatusCodeEquals(self, response, status_code):
        """
        Assert that the response has the given status code.  If not, give a
        useful error mesage.
        """
        self.assertEqual(response.status_code, status_code,
                          'Status Code: %i != %i\nData: %s' % (
                response.status_code, status_code,
                response.content or response.get('Location', '')))

    def assertRequiresAuthentication(self, url, *args,
                                     **kwargs):
        """
        Assert that the given URL requires the user to be authenticated.

        If additional arguments are passed, they are passed to the Client.get
        method

        If keyword arguments are present, they're passed to Client.login before
        the URL is accessed.

        @param url_or_reverse: the URL to access
        """
        c = Client()

        if kwargs:
            c.login(**kwargs)

        response = c.get(url, *args)
        if args and args[0]:
            url = '%s?%s' % (url, urlencode(args[0]))
        self.assertStatusCodeEquals(response, 302)
        self.assertEqual(response['Location'],
                          'http://%s%s?next=%s' %
                          ('testserver',
                           settings.LOGIN_URL,
                           quote_plus(url, safe='/')))
    def _clear_index(self):
        """Clears the search index."""
        backend = connections['default'].get_backend()
        backend.clear()

    def _update_index(self):
        """Updates the search index."""
        backend = connections['default'].get_backend()
        index = connections['default'].get_unified_index().get_index(Video)
        qs = index.index_queryset()
        if qs:
            backend.update(index, qs)
        
    def _rebuild_index(self):
        """Clears and then updates the search index."""
        self._clear_index()
        self._update_index()


# -----------------------------------------------------------------------------
# Feed tests
# -----------------------------------------------------------------------------

class FeedModelTestCase(BaseTestCase):

    fixtures = BaseTestCase.fixtures + ['feeds']

    def test_video_service(self):
        """
        Feed.video_service() should return the name of the video service that
        the feed comes from.  If it doesn't come from a known service, it
        should return None.
        """
        services = (
            ('YouTube',
             'http://gdata.youtube.com/feeds/base/standardfeeds/top_rated'),
            ('YouTube',
             'http://www.youtube.com/rss/user/test/video.rss'),
            ('blip.tv', 'http://miropcf.blip.tv/rss'),
            ('Vimeo', 'http://vimeo.com/tag:miro/rss'))

        feed = Feed.objects.get(pk=1)
        for service, url in services:
            feed.feed_url = url
            self.assertEqual(feed.video_service(), service,
                              '%s was incorrectly described as %s' %
                              (url, feed.video_service()))


# -----------------------------------------------------------------------------
# View tests
# -----------------------------------------------------------------------------


class ViewTestCase(BaseTestCase):

    fixtures = BaseTestCase.fixtures + ['categories', 'feeds', 'videos',
                                        'watched']

    def test_index(self):
        """
        The index view should render the 'localtv/index.html'.  The context
        should include 10 featured videos, 10 popular videos, 10 new views, and
        the base categories (those without parents).
        """
        Watch.objects.update(timestamp=datetime.datetime.now())
        # Rebuild index to work around https://bitbucket.org/mchaput/whoosh/issue/237
        #haystack_batch_update.apply(args=(Video._meta.app_label,
        #                                  Video._meta.module_name))
        self._rebuild_index()

        c = Client()
        response = c.get(reverse('localtv_index'))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/index.html')
        featured = list(Video.objects.get_featured_videos(self.site_settings))
        self.assertEqual(list(response.context['featured_videos']),
                          featured)
        popular = response.context['popular_videos']
        self.assertIsInstance(popular, NormalizedVideoList)
        self.assertEqual(len(popular),
                         Video.objects.filter(status=Video.ACTIVE).count())
        popular_list = list(popular.queryset)
        self.assertEqual(popular_list, sorted(popular_list, reverse=True,
                                         key=lambda v: v.watch_count))
        self.assertEqual(list(response.context['new_videos']),
                          list(Video.objects.get_latest_videos(self.site_settings)))
        self.assertEqual(list(response.context['comments']), [])

    def test_index_recent_comments_skips_rejected_videos(self):
        """
        Recent comments should only include approved videos.
        """
        unapproved = Video.objects.filter(
            status=Video.UNAPPROVED)[0]
        approved = Video.objects.filter(
            status=Video.ACTIVE)[0]
        rejected = Video.objects.filter(
            status=Video.REJECTED)[0]
        for video in unapproved, approved, rejected:
            Comment.objects.create(
                site=self.site_settings.site,
                content_object=video,
                comment='Test Comment')

        c = Client()
        response = c.get(reverse('localtv_index'))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(len(response.context['comments']), 1)
        self.assertEqual(response.context['comments'][0].content_object,
                          approved)

        approved.status = Video.REJECTED
        approved.save()

        c = Client()
        response = c.get(reverse('localtv_index'))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(len(response.context['comments']), 0)

    def test_about(self):
        """
        The about view should render the 'localtv/about.html' template.
        """
        c = Client()
        response = c.get(reverse('localtv_about'))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/about.html')

    def test_view_video(self):
        """
        The view_video view should render the 'localtv/view_video.html'
        template.  It should include the current video, and a QuerySet of other
        popular videos.
        """
        for watched in Watch.objects.all():
            watched.timestamp = datetime.datetime.now() # so that they're
                                                        # recent
            watched.save()
        self._rebuild_index()

        video = Video.objects.get(pk=20)

        c = Client()
        response = c.get(video.get_absolute_url())
        self.assertStatusCodeEquals(response, 200)
        self.assertTrue('localtv/view_video.html' in [
                template.name for template in response.templates])
        self.assertEqual(response.context['current_video'], video)
        self.assertTrue('popular_videos' in response.context)

    def test_view_video_admins_see_rejected(self):
        """
        The view_video view should return a 404 for rejected videos, unless the
        user is an admin.
        """
        video = Video.objects.get(pk=1)

        c = Client()
        response = c.get(video.get_absolute_url(), follow=True)
        self.assertStatusCodeEquals(response, 404)

        c.login(username='admin', password='admin')
        response = c.get(video.get_absolute_url())
        self.assertStatusCodeEquals(response, 200)

    def test_view_video_slug_redirect(self):
        """
        The view_video view should redirect the user to the URL with the slug
        if the URL doesn't include it or includes the wrong one.
        """
        video = Video.objects.get(pk=20)

        c = Client()
        response = c.get(reverse('localtv_view_video',
                                 args=[20, 'wrong-slug']))
        self.assertStatusCodeEquals(response, 302)
        self.assertEqual(response['Location'],
                          'http://%s%s' % (
                'testserver',
                video.get_absolute_url()))

        response = c.get(reverse('localtv_view_video',
                                 args=[20, '']))
        self.assertStatusCodeEquals(response, 302)
        self.assertEqual(response['Location'],
                          'http://%s%s' % (
                'testserver',
                video.get_absolute_url()))

    def test_view_video_category(self):
        """
        Mostly just checks that we're in the right place. Some additional
        checks were removed since they weren't checking in a repeatable manner.
        """
        video = Video.objects.get(pk=20)
        video.categories = [2]
        video.save()

        c = Client()
        response = c.get(video.get_absolute_url())
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.context['category'].pk, 2)

    def test_view_video_category_referer(self):
        """
        If the view_video referrer was a category page, that category should be
        the one included in the template.
        """
        video = Video.objects.get(pk=20)
        video.categories = [1, 2]
        video.save()

        c = Client()
        response = c.get(video.get_absolute_url(),
                         HTTP_HOST='testserver',
                         HTTP_REFERER='http://%s%s' % (
                'testserver',
                reverse('localtv_category',
                        args=['miro'])))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.context['category'].pk, 1)

    def assertSearchResults(self, response, expected_sqs,
                            expected_object_count, expected_page_num):
        paginator = response.context['paginator']
        per_page = paginator.per_page
        page_num = response.context['page_obj'].number
        videos = list(response.context['video_list'])
        expected_sqs_results = [r.object for r in expected_sqs if
                                r.object.status == Video.ACTIVE]
        start = (page_num - 1) * per_page
        end = page_num * per_page

        self.assertEqual(page_num, expected_page_num)
        self.assertEqual(len(paginator.object_list),
                          expected_object_count)
        self.assertEqual(videos, expected_sqs_results[start:end])

    def test_video_search(self):
        """
        The video_search view should take a GET['q'] and search through the
        videos.  It should render the
        'localtv/video_listing_search.html' template.
        """
        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': 'blender'}) # lots of Blender videos in the test
                                           # data
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/video_listing_search.html')
        self.assertSearchResults(response, 
                                 SearchQuerySet().models(models.Video).filter(
                site=1, content='blender'),
                                 16, 1)

    def test_video_search_phrase(self):
        """
        Phrases in quotes should be searched for as a phrase.
        """
        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': '"making of elephants"'})
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/video_listing_search.html')
        self.assertSearchResults(response,
                                 SearchQuerySet().models(models.Video).filter(
                site=1, content='making of elephants'),
                                 4, 1)

    def test_video_search_no_query(self):
        """
        The video_search view should render the
        'localtv/video_listing_search.html' template.
        """
        c = Client()
        response = c.get(reverse('localtv_search'))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/video_listing_search.html')

    def test_video_search_pagination(self):
        """
        The video_search view should take a GET['page'] argument which shows
        the next page of search results.
        """
        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': 'blender',
                          'page': 2})

        self.assertStatusCodeEquals(response, 200)
        self.assertSearchResults(response,
                                 SearchQuerySet().models(models.Video).filter(
                site=1, content='blender'),
                                 16, 2)


    def test_video_search_includes_tags(self):
        """
        The video_search view should search the tags for videos.
        """
        video = Video.objects.get(pk=20)
        video.tags = 'tag1 tag2'
        video.save()

        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': 'tag1'})
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.context['page_obj'].number, 1)
        self.assertEqual(response.context['paginator'].num_pages, 1)
        self.assertEqual(list(response.context['video_list']),
                          [video])

        response = c.get(reverse('localtv_search'),
                          {'q': 'tag2'})
        self.assertEqual(list(response.context['video_list']),
                          [video])

        response = c.get(reverse('localtv_search'),
                         {'q': 'tag2 tag1'})
        self.assertEqual(list(response.context['video_list']),
                          [video])

    def test_video_search_includes_categories(self):
        """
        The video_search view should search the category for videos.
        """
        video = Video.objects.get(pk=20)
        video.categories = [2] # Linux (child of Miro)
        video.save()

        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': 'Miro'})
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.context['page_obj'].number, 1)
        self.assertEqual(response.context['paginator'].num_pages, 1)
        self.assertEqual(list(response.context['video_list']),
                          [video])

        response = c.get(reverse('localtv_search'),
                         {'q': 'Linux'})
        self.assertEqual(list(response.context['video_list']),
                          [video])

        response = c.get(reverse('localtv_search'),
                         {'q': 'Miro Linux'})
        self.assertEqual(list(response.context['video_list']),
                          [video])

    def test_video_search_includes_user(self):
        """
        The video_search view should search the user who submitted videos.
        """
        video = Video.objects.get(pk=20)
        video.user = User.objects.get(username='superuser')
        video.user.first_name = 'firstname'
        video.user.last_name = 'lastname'
        video.user.save()
        video.save()

        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': 'superuser'}) # username
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.context['page_obj'].number, 1)
        self.assertEqual(response.context['paginator'].num_pages, 1)
        self.assertEqual(list(response.context['video_list']),
                          [video])

        response = c.get(reverse('localtv_search'),
                         {'q': 'firstname'}) # first name
        self.assertEqual(list(response.context['video_list']),
                          [video])

        response = c.get(reverse('localtv_search'),
                         {'q': 'lastname'}) # last name
        self.assertEqual(list(response.context['video_list']),
                          [video])

    def test_video_search_includes_video_service_user(self):
        """
        The video_search view should search the video service user for videos.
        """
        video = Video.objects.get(pk=20)
        video.video_service_user = 'Video_service_user'
        video.save()

        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': 'video_service_user'})
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.context['page_obj'].number, 1)
        self.assertEqual(response.context['paginator'].num_pages, 1)
        self.assertEqual(list(response.context['video_list']),
                          [video])

    def test_video_search_includes_feed_name(self):
        """
        The video_search view should search the feed name for videos.
        """
        video = Video.objects.get(pk=20)
        # feed is miropcf

        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': 'miropcf'})
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.context['page_obj'].number, 1)
        self.assertEqual(response.context['paginator'].num_pages, 1)
        self.assertEqual(list(response.context['video_list']),
                          [video])

    def test_video_search_exclude_terms(self):
        """
        The video_search view should exclude terms that start with - (hyphen).
        """
        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': '-blender'})
        self.assertStatusCodeEquals(response, 200)
        self.assertSearchResults(response,
                                 SearchQuerySet().models(models.Video).filter(
                site=1).exclude(content='blender'),
                                 7, 1)

    def test_video_search_unicode(self):
        """
        The video_search view should handle Unicode strings.
        """
        c = Client()
        response = c.get(reverse('localtv_search'),
                         {'q': u'espa\xf1a'})
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.context['page_obj'].number, 1)
        self.assertEqual(response.context['paginator'].num_pages, 1)
        self.assertEqual(list(response.context['video_list']), [])

    def test_category_index(self):
        """
        The category_index view should render the
        'localtv/categories.html' template and include the root
        categories (those without parents).
        """
        c = Client()
        response = c.get(reverse('localtv_category_index'))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/categories.html')
        self.assertEqual(response.context['paginator'].num_pages, 1)
        self.assertEqual(list(response.context['page_obj'].object_list),
                          list(Category.objects.filter(parent=None,
                               site=Site.objects.get_current())))

    def test_category(self):
        """
        The category view should render the 'localtv/category.html'
        template, and include the appropriate category.
        """
        category = Category.objects.get(slug='miro')
        for video in models.Video.objects.filter(status=models.Video.ACTIVE):
            video.categories = [1] # Linux
            video.save()
        c = Client()
        response = c.get(category.get_absolute_url())
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/category.html')
        self.assertEqual(response.context['category'], category)
        videos = list(models.Video.objects.with_best_date().filter(
                status=models.Video.ACTIVE).order_by('-best_date')[:15])
        self.assertEqual(videos, sorted(videos, key=lambda v: v.when(),
                                        reverse=True))
        self.assertEqual(response.context['page_obj'].object_list,
                         videos)

    def test_author_index(self):
        """
        The author_index view should render the
        'localtv/author_list.html' template and include the authors in
        the context.
        """
        c = Client()
        response = c.get(reverse('localtv_author_index'))
        self.assertEqual(response.templates[0].name,
                          'localtv/author_list.html')
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(list(response.context['authors']),
                          list(User.objects.all()))

    def test_author(self):
        """
        The author view should render the 'localtv/author.html'
        template and include the author and their videos.
        """
        author = User.objects.get(username='admin')
        c = Client()
        response = c.get(reverse('localtv_author',
                                 args=[author.pk]))
        videos = list(response.context['video_list'])
        site_settings = SiteSettings.objects.get_current()
        expected = list(
            Video.objects.get_latest_videos(site_settings).filter(
                Q(user=author) | Q(authors=author)
            ).distinct().order_by('-best_date')
        )
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/author.html')
        self.assertEqual(response.context['author'], author)
        self.assertEqual(len(videos), 2)
        self.assertEqual(videos, expected)


# -----------------------------------------------------------------------------
# Listing Views tests
# -----------------------------------------------------------------------------


class ListingViewTestCase(BaseTestCase):

    fixtures = BaseTestCase.fixtures + ['feeds', 'videos', 'watched']

    def test_latest_videos(self):
        """
        The new_videos view should render the
        'localtv/video_listing_new.html' template and include the new
        videos.
        """
        c = Client()
        response = c.get(reverse('localtv_list_new'))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/video_listing_new.html')
        self.assertEqual(response.context['paginator'].num_pages, 2)
        self.assertEqual(len(response.context['page_obj'].object_list), 15)
        self.assertEqual(list(response.context['page_obj'].object_list),
                          list(Video.objects.get_latest_videos(
                              self.site_settings)[:15]))

    def test_popular_videos(self):
        """
        The popular_videos view should render the
        'localtv/video_listing_popular.html' template and include the
        all the videos, sorted by popularity.
        """
        Watch.objects.update(timestamp=datetime.datetime.now())
        haystack_batch_update.apply(args=(Video._meta.app_label,
                                          Video._meta.module_name))

        c = Client()
        response = c.get(reverse('localtv_list_popular'))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/video_listing_popular.html')
        self.assertEqual(response.context['paginator'].count, 23)

        results = response.context['page_obj'].object_list
        watch_qs = Watch.objects.filter(
               timestamp__gte=datetime.datetime.now() - datetime.timedelta(7))
        expected = sorted(results, reverse=True,
                          key=lambda v: watch_qs.filter(video=v).count())
        self.assertEqual(list(results), list(expected))

    def test_featured_videos(self):
        """
        The featured_videos view should render the
        'localtv/video_listing_featured.html' template and include the
        featured videos.
        """
        c = Client()
        response = c.get(reverse('localtv_list_featured'))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/video_listing_featured.html')
        self.assertEqual(response.context['paginator'].count, 2)
        self.assertEqual(list(response.context['page_obj'].object_list),
                          list(Video.objects.filter(status=Video.ACTIVE,
                               last_featured__isnull=False)))

    def test_tag_videos(self):
        """
        The tag_videos view should render the
        'localtv/video_listing_tag.html' template and include the
        tagged videos.
        """
        video = Video.objects.get(pk=20)
        video.tags = 'tag1'
        video.save()

        c = Client()
        response = c.get(reverse('localtv_list_tag',
                         kwargs={'name': 'tag1'}))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/video_listing_tag.html')
        self.assertEqual(response.context['paginator'].num_pages, 1)
        self.assertEqual(len(response.context['page_obj'].object_list), 1)
        self.assertEqual(list(response.context['page_obj'].object_list),
                          [video])

    def test_feed_videos(self):
        """
        The feed_videos view should render the
        'localtv/video_listing_feed.html' template and include the
        videos from the given feed.
        """
        feed = Feed.objects.get(pk=1)

        c = Client()
        response = c.get(reverse('localtv_list_feed',
                                 args=[feed.pk]))
        self.assertStatusCodeEquals(response, 200)
        self.assertEqual(response.templates[0].name,
                          'localtv/video_listing_feed.html')
        self.assertEqual(response.context['paginator'].num_pages, 1)
        self.assertEqual(len(response.context['page_obj'].object_list), 1)
        self.assertEqual(list(response.context['page_obj'].object_list),
                          list(feed.video_set.filter(
                    status=Video.ACTIVE)))


# -----------------------------------------------------------------------------
# Comment moderation tests
# -----------------------------------------------------------------------------

class CommentModerationTestCase(BaseTestCase):

    fixtures = BaseTestCase.fixtures + ['feeds', 'videos']

    def setUp(self):
        BaseTestCase.setUp(self)
        self.old_COMMENTS_APP = getattr(settings, 'COMMENTS_APP', None)
        settings.COMMENTS_APP = 'localtv.comments'
        self.video = Video.objects.get(pk=20)
        self.url = get_form_target()
        if 'captcha' in CommentForm.base_fields:
            del CommentForm.base_fields['captcha']
        self.form = CommentForm(self.video,
                                initial={
                'name': 'postname',
                'email': 'post@email.com',
                'url': 'http://posturl.com/'})
        self.POST_data = self.form.initial
        self.POST_data['comment'] = 'comment string'

    def tearDown(self):
        settings.COMMENTS_APP = self.old_COMMENTS_APP

    def test_deleting_video_deletes_comments(self):
        """
        If the video for a comment is deleted, the comment should be deleted as
        well.
        """
        c = Client()
        c.post(self.url, self.POST_data)
        self.assertEqual(Comment.objects.count(), 1)
        self.video.delete()
        self.assertFalse(Comment.objects.exists())

    def test_comment_does_not_require_email_or_url(self):
        """
        Posting a comment should not require an e-mail address or URL.
        """
        del self.POST_data['email']
        del self.POST_data['url']

        c = Client()
        c.post(self.url, self.POST_data)
        comment = Comment.objects.get()
        self.assertEqual(comment.content_object, self.video)
        self.assertFalse(comment.is_public)
        self.assertEqual(comment.name, 'postname')
        self.assertEqual(comment.email, '')
        self.assertEqual(comment.url, '')

    def test_screen_all_comments_False(self):
        """
        If SiteSettings.screen_all_comments is False, the comment should be
        saved and marked as public.
        """
        self.site_settings.screen_all_comments = False
        self.site_settings.save()

        c = Client()
        c.post(self.url, self.POST_data)

        comment = Comment.objects.get()
        self.assertEqual(comment.content_object, self.video)
        self.assertTrue(comment.is_public)
        self.assertEqual(comment.name, 'postname')
        self.assertEqual(comment.email, 'post@email.com')
        self.assertEqual(comment.url, 'http://posturl.com/')

    def test_screen_all_comments_True(self):
        """
        If SiteSettings.screen_all_comments is True, the comment should be
        moderated (not public).
        """
        notice_type = notification.NoticeType.objects.get(
            label='admin_new_comment')
        for username in 'admin', 'superuser':
            user = User.objects.get(username=username)
            setting = notification.get_notification_setting(user, notice_type,
                                                            "1")
            setting.send = True
            setting.save()

        c = Client()
        c.post(self.url, self.POST_data)

        comment = Comment.objects.get()
        self.assertEqual(comment.content_object, self.video)
        self.assertFalse(comment.is_public)
        self.assertEqual(comment.name, 'postname')
        self.assertEqual(comment.email, 'post@email.com')
        self.assertEqual(comment.url, 'http://posturl.com/')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].recipients(),
                          ['admin@testserver.local',
                           'superuser@testserver.local'])

    def test_screen_all_comments_True_admin(self):
        """
        Even if SiteSettings,screen_all_comments is True, comments from logged
        in admins should not be screened.
        """
        c = Client()
        c.login(username='admin', password='admin')
        c.post(self.url, self.POST_data)

        comment = Comment.objects.get()
        self.assertEqual(comment.content_object, self.video)
        self.assertTrue(comment.is_public)
        comment.delete()

        c.login(username='superuser', password='superuser')
        c.post(self.url, self.POST_data)

        comment = Comment.objects.get()
        self.assertEqual(comment.content_object, self.video)
        self.assertTrue(comment.is_public)

    def test_comments_email_admins_False(self):
        """
        If no admin is subscribed to the 'admin_new_comment' notification, no
        e-mail should be sent when a comment is made.
        """
        c = Client()
        c.post(self.url, self.POST_data)

        self.assertEqual(mail.outbox, [])

    def test_comments_email_admins_True(self):
        """
        If any admins are subscribed to the 'admin_new_comment' notification,
        an e-mail should be sent when a comment is made to each.
        """
        notice_type = notification.NoticeType.objects.get(
            label='admin_new_comment')
        for username in 'admin', 'superuser':
            user = User.objects.get(username=username)
            setting = notification.get_notification_setting(user, notice_type,
                                                            "1")
            setting.send = True
            setting.save()

        c = Client()
        c.post(self.url, self.POST_data)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].recipients(),
                          ['admin@testserver.local',
                           'superuser@testserver.local'])

    def test_comments_required_login_False(self):
        """
        If SiteSettings.comments_required_login is False, comments should be
        allowed by any user.  This is the same test code as
        test_screen_all_comments_False().
        """
        c = Client()
        c.post(self.url, self.POST_data)

        comment = Comment.objects.get()
        self.assertEqual(comment.content_object, self.video)
        self.assertFalse(comment.is_public)
        self.assertEqual(comment.name, 'postname')
        self.assertEqual(comment.email, 'post@email.com')
        self.assertEqual(comment.url, 'http://posturl.com/')

    def test_comments_required_login_True(self):
        """
        If SiteSettings.comments_required_login, making a comment should
        require a logged-in user.
        """
        self.site_settings.comments_required_login = True
        self.site_settings.save()

        c = Client()
        response = c.post(self.url, self.POST_data)
        self.assertStatusCodeEquals(response, 400)
        self.assertEqual(Comment.objects.count(), 0)

        c.login(username='user', password='password')
        c.post(self.url, self.POST_data)

        comment = Comment.objects.get()
        self.assertEqual(comment.content_object, self.video)
        self.assertFalse(comment.is_public)
        self.assertEqual(comment.name, 'Firstname Lastname')
        self.assertEqual(comment.email, 'user@testserver.local')
        self.assertEqual(comment.url, 'http://posturl.com/')

    def test_comments_email_submitter(self):
        """
        If the submitter of a video has the 'video_comment' notificiation
        enabled, an e-mail with the comment should be sent to them.
        """
        video = Video.objects.get(pk=43) # has a user
        form = CommentForm(video,
                           initial={
                'name': 'postname',
                'email': 'post@email.com',
                'url': 'http://posturl.com/'})
        POST_data = form.initial
        POST_data['comment'] = 'comment string'

        # the default is to receive comment e-mails

        self.site_settings.screen_all_comments = False
        self.site_settings.save()

        c = Client()
        response = c.post(self.url, POST_data)
        self.assertStatusCodeEquals(response, 302)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].recipients(),
                          [video.user.email])

    def test_comments_email_submitter_once(self):
        """
        If the submitter of a video is an admin and has both the
        'video_comment' and 'admin_new_comment' notifications, they should only
        receive one e-mail.
        """
        admin = User.objects.get(username='admin')

        notice_type = notification.NoticeType.objects.get(
            label='admin_new_comment')
        setting = notification.get_notification_setting(admin, notice_type,
                                                        "1")
        setting.send = True
        setting.save()

        self.video.user = admin
        self.video.save()

        # the default is to receive comment e-mails

        self.site_settings.screen_all_comments = False
        self.site_settings.save()

        c = Client()
        c.post(self.url, self.POST_data)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].recipients(),
                          [admin.email])

    def test_comments_email_previous_commenter(self):
        """
        If previous comment submitters have the 'comment_post_comment'
        notification enabled, they should get an e-mail when a new comment
        appears on a video they've commented on.
        """
        user = User.objects.get(username='user')

        self.video.user = None
        self.video.save()

        self.site_settings.screen_all_comments = False
        self.site_settings.save()

        c = Client()
        c.login(username='user', password='password')
        self.assertStatusCodeEquals(c.post(self.url, self.POST_data), 302)

        self.assertEqual(len(mail.outbox), 0)

        mail.outbox = []

        c = Client()
        c.login(username='admin', password='admin')
        self.POST_data['comment'] = 'another comment'
        self.assertStatusCodeEquals(c.post(self.url, self.POST_data), 302)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].recipients(),
                          [user.email])


# -----------------------------------------------------------------------------
# Video model tests
# -----------------------------------------------------------------------------

class VideoModelTestCase(BaseTestCase):

    fixtures = BaseTestCase.fixtures + ['feeds', 'videos']

    def test_when(self):
        """
        Video.when() returns the best available date for the video:

        1) when_published (original publish date)
        2) when_approved (when it was made visible on the site)
        3) when_submitted (when it was submitted to the site)
        """
        v = Video.objects.get(pk=11)
        self.assertEqual(v.when(), v.when_published)
        v.when_published = None
        self.assertEqual(v.when(), v.when_approved)
        v.when_approved = None
        self.assertEqual(v.when(), v.when_submitted)

    def test_when_use_original_date_False(self):
        """
        When SiteSettings.use_original_date is False, Video.when() ignores the
        when_published date.
        """
        self.site_settings.use_original_date = False
        self.site_settings.save()
        v = Video.objects.get(pk=11)
        self.assertEqual(v.when(), v.when_approved)


    def test_when_prefix(self):
        """
        Video.when_prefix() returns 'published' if the date is
        when_published, otherwise it returns 'posted'..
        """
        v = Video.objects.get(pk=11)
        self.assertEqual(v.when_prefix(), 'published')
        v.when_published = None
        self.assertEqual(v.when_prefix(), 'posted')

    def test_when_prefix_use_original_date_False(self):
        """
        When SiteSettings.use_original_date is False, Video.when_prefix()
        returns 'posted'.
        """
        self.site_settings.use_original_date = False
        self.site_settings.save()
        v = Video.objects.get(pk=11)
        self.assertEqual(v.when_prefix(), 'posted')

    def test_latest(self):
        """
        Video.objects.get_latest_videos() should return a QuerySet ordered by
        the best available date:

        1) when_published
        2) when_approved
        3) when_submitted

        SearchQuerySet().models(Video).order_by('-best_date_with_published')
        should return the same videos.

        """
        expected_pks = set(Video.objects.filter(status=Video.ACTIVE,
                                                site=self.site_settings.site
                                       ).values_list('pk', flat=True))

        results = list(Video.objects.get_latest_videos(self.site_settings))
        self.assertEqual(set(r.pk for r in results), expected_pks)
        for i in xrange(len(results) - 1):
            self.assertTrue(results[i].when() >= results[i+1].when())

        sqs = SearchQuerySet().models(Video).order_by(
                                      '-best_date_with_published')
        results = list([r.object for r in sqs.load_all()])
        self.assertEqual(set(r.pk for r in results), expected_pks)
        for i in xrange(len(results) - 1):
            self.assertTrue(results[i].when() >= results[i+1].when())

    def test_latest_use_original_date_False(self):
        """
        When SiteSettings.use_original_date is False,
        Video.objects.get_latest_videos() should ignore the when_published date.

        SearchQuerySet().models(Video).order_by('-best_date') should return the
        same videos.

        """
        expected_pks = set(Video.objects.filter(status=Video.ACTIVE,
                                                site=self.site_settings.site
                                       ).values_list('pk', flat=True))

        self.site_settings.use_original_date = False
        self.site_settings.save()

        results = list(Video.objects.get_latest_videos(self.site_settings))
        self.assertEqual(set(r.pk for r in results), expected_pks)
        for i in xrange(len(results) - 1):
            self.assertTrue(results[i].when() >= results[i+1].when())

        sqs = SearchQuerySet().models(Video).order_by(
                                      '-best_date')
        results = list([r.object for r in sqs.load_all()])
        self.assertEqual(set(r.pk for r in results), expected_pks)
        for i in xrange(len(results) - 1):
            self.assertTrue(results[i].when() >= results[i+1].when())

# -----------------------------------------------------------------------------
# Watch model tests
# -----------------------------------------------------------------------------

class WatchModelTestCase(BaseTestCase):

    fixtures = BaseTestCase.fixtures + ['feeds', 'videos']

    def test_add(self):
        """
        Watch.add(request, video) should add a Watch object to the database for
        the given video.
        """
        request = HttpRequest()
        request.META['REMOTE_ADDR'] = '123.123.123.123'
        request.user = User.objects.get(username='user')

        video = Video.objects.get(pk=1)

        Watch.add(request, video)

        watch = Watch.objects.get()
        self.assertEqual(watch.video, video)
        self.assertTrue(watch.timestamp - datetime.datetime.now() <
                        datetime.timedelta(seconds=1))
        self.assertEqual(watch.user, request.user)
        self.assertEqual(watch.ip_address, request.META['REMOTE_ADDR'])

    def test_add_unauthenticated(self):
        """
        Unauthenticated requests should add a Watch object with user set to
        None.
        """
        request = HttpRequest()
        request.META['REMOTE_ADDR'] = '123.123.123.123'

        video = Video.objects.get(pk=1)

        Watch.add(request, video)

        watch = Watch.objects.get()
        self.assertEqual(watch.video, video)
        self.assertTrue(watch.timestamp - datetime.datetime.now() <
                        datetime.timedelta(seconds=1))
        self.assertEqual(watch.user, None)
        self.assertEqual(watch.ip_address, request.META['REMOTE_ADDR'])

    def test_add_invalid_ip(self):
        """
        Requests with an invalid IP address should not raise an error.  The IP
        address should be saved as 0.0.0.0.
        """
        request = HttpRequest()
        request.META['REMOTE_ADDR'] = 'unknown'

        video = Video.objects.get(pk=1)

        Watch.add(request, video)

        w = Watch.objects.get()
        self.assertEqual(w.video, video)
        self.assertEqual(w.ip_address, '0.0.0.0')

    def test_add_robot(self):
        """
        Requests from Robots (Googlebot, Baiduspider, &c) shouldn't count as
        watches.
        """
        request = HttpRequest()
        request.META['HTTP_USER_AGENT'] = 'Mozilla/5.0 Googlebot'

        video = Video.objects.get(pk=1)

        Watch.add(request, video)

        request = HttpRequest()
        request.META['HTTP_USER_AGENT'] = 'Mozilla/5.0 BaiduSpider'

        Watch.add(request, video)

        self.assertEqual(Watch.objects.count(), 0)


class TestWmodeFilter(BaseTestCase):
    def test_add_transparent_wmode_to_object(self):
        input = "<object></object>"
        output = '<object><param name="wmode" value="transparent"></object>'
        self.assertEqual(output,
                         localtv.templatetags.filters.wmode_transparent(input))

    def test_add_transparent_wmode_to_two_objects(self):
        input = "<object></object>"
        output = '<object><param name="wmode" value="transparent"></object>'
        self.assertEqual(output + output,
                         localtv.templatetags.filters.wmode_transparent(input + input))

    def test_add_transparent_wmode_to_embed(self):
        input = '<embed type="application/x-shockwave-flash"></embed>'
        output = '<embed type="application/x-shockwave-flash" wmode="transparent"></embed>'
        self.assertEqual(output,
                         localtv.templatetags.filters.wmode_transparent(input))
                
class LegacyFeedViewTestCase(BaseTestCase):

    fixtures = BaseTestCase.fixtures + ['feeds', 'videos', 'categories']

    def test_feed_views_respect_count_when_set(self):
        fake_request = self.factory.get('?count=10')
        feed = localtv.feeds.views.NewVideosFeed(json=False)
        obj = feed.get_object(fake_request)
        self.assertEqual(10, len(feed.items(obj)))

    def test_feed_views_ignore_count_when_nonsense(self):
        fake_request = self.factory.get('?count=nonsense')
        feed = localtv.feeds.views.NewVideosFeed(json=False)
        obj = feed.get_object(fake_request)
        # 23, because that's the number of videos in the fixture
        self.assertEqual(23, len(feed.items(obj)))

    def test_feed_views_ignore_count_when_empty(self):
        fake_request = self.factory.get('')
        feed = localtv.feeds.views.NewVideosFeed(json=False)
        obj = feed.get_object(fake_request)
        # 23, because that's the number of videos in the fixture
        self.assertEqual(23, len(feed.items(obj)))

    def test_category_feed_renders_at_all(self):
        fake_request = self.factory.get('?count=10')
        view = localtv.feeds.views.CategoryVideosFeed()
        response = view(fake_request, slug='linux')
        self.assertEqual(200, response.status_code)

    def test_feed_views_respect_count_when_set_integration(self):
        # Put 3 videos into the Linux category
        linux_category = Category.objects.get(slug='linux')
        three_vids = Video.objects.get_latest_videos(
            self.site_settings)[:3]
        self.assertEqual(len(three_vids), 3)
        for vid in three_vids:
            vid.categories.add(linux_category)
            vid.status = Video.ACTIVE
            vid.save()
        self.assertEqual(linux_category.approved_set.count(), 3)
        # Do a GET for the first 2 in the feed
        fake_request = self.factory.get('?count=2')
        view = localtv.feeds.views.CategoryVideosFeed()
        response = view(fake_request, slug='linux')
        self.assertEqual(200, response.status_code)
        parsed = feedparser.parse(response.content)
        items_from_first_GET = parsed['items']
        self.assertEqual(2, len(items_from_first_GET))

        # Do a GET for the next "2" (just 1 left)
        fake_request = self.factory.get('?count=2&start-index=2')
        view = localtv.feeds.views.CategoryVideosFeed()
        response = view(fake_request, slug='linux')
        self.assertEqual(200, response.status_code)
        parsed = feedparser.parse(response.content)
        items_from_second_GET = parsed['items']
        self.assertEqual(1, len(items_from_second_GET))
