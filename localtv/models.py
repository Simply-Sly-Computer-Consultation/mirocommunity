import cgi
import datetime
import httplib
import re
import urllib
import urllib2
import urlparse
import Image
import StringIO

from django.db import models
from django.contrib import admin
from django.contrib.sites.models import Site
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.forms.fields import slug_re
from django.template import mark_safe
from django.utils.html import strip_tags

import feedparser
import vidscraper


# the difference between unapproved and rejected is that unapproved simply
# hasn't been looked at by an administrator yet.
VIDEO_STATUS_UNAPPROVED = FEED_STATUS_UNAPPROVED =0
VIDEO_STATUS_ACTIVE = FEED_STATUS_ACTIVE = 1
VIDEO_STATUS_REJECTED = FEED_STATUS_REJECTED = 2

VIDEO_STATUSES = FEED_STATUSES = (
    (VIDEO_STATUS_UNAPPROVED, 'Unapproved'),
    (VIDEO_STATUS_ACTIVE, 'Active'),
    (VIDEO_STATUS_REJECTED, 'Rejected'))

SITE_STATUS_DISABLED = 0
SITE_STATUS_ACTIVE = 1

SITE_STATUSES = (
    (SITE_STATUS_DISABLED, 'Disabled'),
    (SITE_STATUS_ACTIVE, 'Active'))

VIDEO_THUMB_SIZES = [
    (500, 281), # featured on frontpage
    (142, 104)]

VIDEO_USER_REGEXES = (
    ('YouTube', r'http://(www\.)?youtube\.com/rss/user/.+/videos\.rss'),
    ('YouTube', r'http://gdata\.youtube\.com/feeds/base/videos/-/.+'),
    ('blip.tv', r'http://.+\.blip\.tv/\?skin=rss'),
    ('blip.tv', r'http://.+\.blip\.tv/rss'),
    ('Vimeo', r'http://www\.vimeo\.com/user:[0-9]+/clips/rss'))

class Error(Exception): pass
class CannotOpenImageUrl(Error): pass


class OpenIdUser(models.Model):
    """
    Custom openid user authentication model.  Presently does not match
    up to Django's contrib.auth.models.User model, probably should be
    adjusted to do so eventually.

    Login and registration functionality provided in localtv.openid
    and its submodules.

    Fields:
      - url: URL that this user is identified by
      - user: the Django User object that this is a valid login for
    """
    user = models.OneToOneField('auth.User')
    url = models.URLField(verify_exists=False, unique=True)

    def __unicode__(self):
        return "%s <%s>" % (self.user.username, self.user.email)


class SiteLocation(models.Model):
    """
    An extension to the django.contrib.sites site model, providing
    localtv-specific data.

    Fields:
     - site: A link to the django.contrib.sites.models.Site object
     - logo: custom logo image for this site
     - background: custom background image for this site (unused?)
     - admins: a collection of Users who have access to administrate this
       sitelocation
     - status: one of SITE_STATUSES; either disabled or active
     - sidebar_html: custom html to appear on the right sidebar of many
       user-facing pages.  Can be whatever's most appropriate for the owners of
       said site.
     - footer_html: HTML that appears at the bottom of most user-facing pages.
       Can be whatever's most appropriate for the owners of said site.
     - about_html: HTML to display on the subsite's about page
     - tagline: displays below the subsite's title on most user-facing pages
     - css: The intention here is to allow subsites to paste in their own CSS
       here from the admin.  Not used presently, though eventually it should
       be.
     - frontpage_style: The style of the frontpage.  Either one of:
        * list
        * scrolling
        * categorized
     - display_submit_button: whether or not we should allow users to see that
       they can submit videos or not (doesn't affect whether or not they
       actually can though)
     - submission_requires_login: whether or not users need to log in to submit
       videos.
    """
    site = models.ForeignKey(Site, unique=True)
    logo = models.ImageField(upload_to='localtv/site_logos', blank=True)
    background = models.ImageField(upload_to='localtv/site_backgrounds',
                                   blank=True)
    admins_user = models.ManyToManyField('auth.User', blank=True,
                                         related_name='admin_for')
    status = models.IntegerField(
        choices=SITE_STATUSES, default=SITE_STATUS_ACTIVE)
    sidebar_html = models.TextField(blank=True)
    footer_html = models.TextField(blank=True)
    about_html = models.TextField(blank=True)
    tagline = models.CharField(max_length=250, blank=True)
    css = models.TextField(blank=True)
    frontpage_style = models.CharField(max_length=32, default="list")
    display_submit_button = models.BooleanField(default=True)
    submission_requires_login = models.BooleanField(default=False)

    def __unicode__(self):
        return self.site.name


    def user_is_admin(self, user):
        """
        Return True if the given User is an admin for this SiteLocation.
        """
        if not user.is_authenticated() or not user.is_active:
            return False

        if user.is_superuser:
            return True

        for sitelocation in user.admin_for.all():
            if self == sitelocation:
                return True

        return False
    
class Tag(models.Model):
    """
    Tags for videos.

    Presently apply to all sitelocations.  Maybe eventually only certain tags
    should apply to certain sitelocations?

    Fields:
      - name: name of this tag
    """
    name = models.CharField(max_length=25)

    def __unicode__(self):
        return self.name


class Feed(models.Model):
    """
    Feed to pull videos in from.

    If the same feed is used on two different subsites, they will require two
    separate entries here.

    Fields:
      - feed_url: The location of this field
      - site: which site this feed belongs to
      - name: human readable name for this feed
      - webpage: webpage that this feed's content is associated with
      - description: human readable description of this item
      - last_updated: last time we ran self.update_items()
      - when_submitted: when this feed was first registered on this site
      - status: one of FEED_STATUSES, either unapproved, active, or rejected
      - etag: used to see whether or not the feed has changed since our last
        update.
      - auto_approve: whether or not to set all videos in this feed to approved
        during the import process
      - user: a user that submitted this feed, if any
      - auto_categories: categories that are automatically applied to videos on
        import
      - auto_authors: authors that are automatically applied to videos on
        import
    """
    feed_url = models.URLField(verify_exists=False)
    site = models.ForeignKey(Site)
    name = models.CharField(max_length=250)
    webpage = models.URLField(verify_exists=False, blank=True)
    description = models.TextField()
    last_updated = models.DateTimeField()
    when_submitted = models.DateTimeField(auto_now_add=True)
    status = models.IntegerField(choices=FEED_STATUSES)
    etag = models.CharField(max_length=250, blank=True)
    auto_approve = models.BooleanField(default=False)
    user = models.ForeignKey('auth.User', null=True, blank=True)
    auto_categories = models.ManyToManyField("Category", blank=True)
    auto_authors = models.ManyToManyField("Author", blank=True)

    class Meta:
        unique_together = (
            ('feed_url', 'site'))

    def __unicode__(self):
        return self.name

    def is_user(self):
        for service, regexp in VIDEO_USER_REGEXES:
            if re.search(regexp, self.feed_url, re.I):
                return service
        return False

    def update_items(self, verbose=False):
        """
        Fetch and import new videos from this feed.
        """
        from localtv import miroguide_util, util

        if self.auto_approve:
            initial_video_status = VIDEO_STATUS_ACTIVE
        else:
            initial_video_status = VIDEO_STATUS_UNAPPROVED

        parsed_feed = feedparser.parse(self.feed_url, etag=self.etag)
        for entry in parsed_feed['entries']:
            skip = False
            guid = entry.get('guid')
            if guid is not None and Video.objects.filter(
                feed=self, guid=guid).count():
                skip = True
            link = entry.get('link')
            if link is not None and Video.objects.filter(
                    feed=self, website_url=link).count():
                skip = True
            if skip:
                if verbose:
                    print "Skipping %s" % entry['title']
                continue

            file_url = None
            embed_code = None
            flash_enclosure_url = None
            publish_date = None

            video_enclosure = miroguide_util.get_first_video_enclosure(entry)
            if video_enclosure:
                file_url = video_enclosure['href']

            if link:
                try:
                    scraped_data = vidscraper.auto_scrape(
                        link,
                        fields=['file_url', 'embed', 'flash_enclosure_url',
                                'publish_date'])
                    if not file_url:
                        if not scraped_data.get('file_url_is_flaky'):
                            file_url = scraped_data.get('file_url')
                    embed_code = scraped_data.get('embed')
                    flash_enclosure_url = scraped_data.get(
                        'flash_enclosure_url')
                    publish_date = scraped_data.get('publish_date')
                except vidscraper.errors.Error, e:
                    if verbose:
                        print "Vidscraper error: %s" % e

            if not (file_url or embed_code):
                if verbose:
                    print (
                        "Skipping %s because it lacks file_url "
                        "or embed_code") % entry['title']
                continue

            video = Video(
                name=entry['title'],
                site=self.site,
                description=entry.get('summary', ''),
                file_url=file_url or '',
                embed_code=embed_code or '',
                flash_enclosure_url=flash_enclosure_url or '',
                when_submitted=datetime.datetime.now(),
                when_approved=datetime.datetime.now(),
                when_published=publish_date,
                status=initial_video_status,
                feed=self,
                website_url=entry.get('link', ''),
                thumbnail_url=miroguide_util.get_thumbnail_url(entry) or '')

            video.strip_description()
            video.save()

            try:
                video.save_thumbnail()
            except CannotOpenImageUrl:
                print "Can't get the thumbnail for %s at %s" % (
                    video.id, video.thumbnail_url)

            if entry.get('tags'):
                entry_tags = [
                    tag['term'] for tag in entry['tags']
                    if len(tag['term']) <= 25
                    and len(tag['term']) > 0
                    and slug_re.match(tag['term'])]
                if entry_tags:
                    tags = util.get_or_create_tags(entry_tags)

                    for tag in tags:
                        video.tags.add(tag)

            for category in self.auto_categories.all():
                video.categories.add(category)

            for author in self.auto_authors.all():
                video.authors.add(author)

        self.etag = parsed_feed.get('etag') or ''
        self.last_updated = datetime.datetime.now()
        self.save()


class Category(models.Model):
    """
    A category for videos to be contained in.

    Categoies and tags aren't too different functionally, but categories are
    more strict as they can't be defined by visitors.  Categories can also be
    hierarchical.

    Fields:
     - site: A link to the django.contrib.sites.models.Site object this object
       is bound to
     - name: Name of this category
     - slug: a slugified verison of the name, used to create more friendly URLs
     - logo: An image to associate with this category
     - description: human readable description of this item
     - parent: Reference to another Category.  Allows you to have heirarchical
       categories.
    """
    site = models.ForeignKey(Site)
    name = models.CharField(
        max_length=80, verbose_name='Category Name',
        help_text=("The name is used to identify the "
                   "category almost everywhere; for "
                   "example under the post or in the "
                   "category widget."))
    slug = models.SlugField(
        verbose_name='Category Slug',
        help_text=('The "slug" is the URL-friendly version '
                   "of the name.  It is usually lower-case "
                   "and contains only letters, numbers and "
                   "hyphens."))
    logo = models.ImageField(
        upload_to="localtv/category_logos", blank=True,
        verbose_name='Thumbnail/Logo',
        help_text=("For example: a leaf for 'environment' "
                   "or the logo of a university "
                   "department."))
    description = models.TextField(
        blank=True, verbose_name='Description (HTML)',
        help_text=("The description is not prominent "
                   "by default, but some themes may "
                   "show it."))
    parent = models.ForeignKey(
        'self', blank=True, null=True,
        related_name='child_set',
        verbose_name='Category Parent',
        help_text=("Categories, unlike tags, can have a "
                   "hierarchy."))

    class Meta:
        ordering = ['name']
        unique_together = (
            ('slug', 'site'),
            ('name', 'site'))

    def __unicode__(self):
        return self.name

    def depth(self):
        """
        Returns the number of parents this category has.  Used for indentation.
        """
        depth = 0
        parent = self.parent
        while parent is not None:
            depth += 1
            parent = parent.parent
        return depth

    def dashes(self):
        return mark_safe('&mdash;' * self.depth())

    @models.permalink
    def get_absolute_url(self):
        return ('localtv_subsite_category', [self.slug])

    @classmethod
    def in_order(klass, sitelocation):
        objects = []
        def accumulate(categories):
            for category in categories:
                objects.append(category)
                if category.child_set.count():
                    accumulate(category.child_set.all())
        accumulate(klass.objects.filter(site=sitelocation, parent=None))
        return objects


class CategoryAdmin(admin.ModelAdmin):
    prepopulated_fields = {'slug': ('name',)}


class Author(models.Model):
    """
    The author of a video.

    One of the ambitions of LocalTV is to create some communication between our
    project and the authors of media, so we try and collect this information so
    we can link back to them.

    Fields:
     - site: the site this author is bound to
     - name: name of the author
     - logo: a thumbnail to represent the author by
    """
    site = models.ForeignKey(Site)
    name = models.CharField(max_length=80, verbose_name='Author Name')
    logo = models.ImageField(upload_to="localtv/category_logos", blank=True,
                             verbose_name='Author Image')

    class Meta:
        ordering = ['name']
        unique_together = (
            ('name', 'site'))

    def __unicode__(self):
        return self.name

    @models.permalink
    def get_absolute_url(self):
        return ('localtv_subsite_author', [str(self.id)])


class SavedSearch(models.Model):
    """
    A set of keywords to regularly pull in new videos from.

    There's an administrative interface for doing "live searches"

    Fields:
     - site: site this savedsearch applies to
     - query_string: a whitespace-separated list of words to search for.  Words
       starting with a dash will be processed as negative query terms
     - when_created: date and time that this search was saved.
     - user: the person who saved this search (thus, likely an
       adminsistrator of this subsite)
    """
    site = models.ForeignKey(Site)
    query_string = models.TextField()
    when_created = models.DateTimeField()
    user = models.ForeignKey('auth.User', null=True, blank=True)

    def __unicode__(self):
        return self.query_string

    def update_items(self, verbose=False):
        from localtv import util
        raw_results = vidscraper.metasearch.intersperse_results(
            util.metasearch_from_querystring(
                self.query_string))

        raw_results = [util.MetasearchVideo.create_from_vidscraper_dict(
                result) for result in raw_results]

        raw_results = util.strip_existing_metasearchvideos(
            [result for result in raw_results if result is not None],
            self.site)

        for result in raw_results:
            result.generate_video_model(self.site.site,
                                        VIDEO_STATUS_UNAPPROVED)


class Video(models.Model):
    """
    Fields:
     - name: Name of this video
     - site: Site this video is attached to
     - description: Video description
     - tags: A list of Tag objects associated with this item
     - categories: Similar to Tags
     - authors: the person/people responsible for this video
     - file_url: The file this object points to (if any) ... if not
       provided, at minimum we need the embed_code for the item.
     - file_url_length: size of the file, in bytes
     - file_url_mimetype: mimetype of the file
     - when_submitted: When this item was first entered into the
       database
     - when_approved: When this item was marked to appear publicly on
       the site
     - when_published: When this file was published at its original
       source (if known)
     - last_featured: last time this item was featured.
     - status: one of localtv.models.VIDEOS_STATUSES
     - feed: which feed this item came from (if any)
     - website_url: The page that this item is associated with.
     - embed_code: code used to embed this item.
     - flash_enclosure_url: Crappy enclosure link that doesn't
       actually point to a url.. the kind crappy flash video sites
       give out when they don't actually want their enclosures to
       point to video files.
     - guid: data used to identify this video
     - has_thumbnail: whether or not this video has a thumbnail
     - thumbnail_url: url to the thumbnail, if such a thing exists
     - thumbnail_extension: extension of the *internal* thumbnail, saved on the
       server (usually paired with the id, so we can determine "1123.jpg" or
       "1186.png"
     - user: if not None, the user who submitted this video
     - search: if not None, the SavedSearch from which this video came
    """
    name = models.CharField(max_length=250)
    site = models.ForeignKey(Site)
    description = models.TextField(blank=True)
    tags = models.ManyToManyField(Tag, blank=True)
    categories = models.ManyToManyField(Category, blank=True)
    authors = models.ManyToManyField(Author, blank=True)
    file_url = models.URLField(verify_exists=False, blank=True)
    file_url_length = models.IntegerField(null=True, blank=True)
    file_url_mimetype = models.CharField(max_length=60, blank=True)
    when_submitted = models.DateTimeField(auto_now_add=True)
    when_approved = models.DateTimeField(null=True, blank=True)
    when_published = models.DateTimeField(null=True, blank=True)
    last_featured = models.DateTimeField(null=True, blank=True)
    status = models.IntegerField(
        choices=VIDEO_STATUSES, default=VIDEO_STATUS_UNAPPROVED)
    feed = models.ForeignKey(Feed, null=True, blank=True)
    website_url = models.URLField(verify_exists=False, blank=True)
    embed_code = models.TextField(blank=True)
    flash_enclosure_url = models.URLField(verify_exists=False, blank=True)
    guid = models.CharField(max_length=250, blank=True)
    has_thumbnail = models.BooleanField(default=False)
    thumbnail_url = models.URLField(
        verify_exists=False, blank=True, max_length=400)
    thumbnail_extension = models.CharField(max_length=8, blank=True)
    user = models.ForeignKey('auth.User', null=True, blank=True)
    search = models.ForeignKey(SavedSearch, null=True, blank=True)

    class Meta:
        ordering = ['-when_submitted']

    def __unicode__(self):
        return self.name

    @models.permalink
    def get_absolute_url(self):
        return ('localtv_view_video', (),
                {'video_id': self.id})

    def try_to_get_file_url_data(self):
        """
        Do a HEAD request on self.file_url to find information about
        self.file_url_length and self.file_url_mimetype

        Note that while this method fills in those attributes, it does *NOT*
        run self.save() ... so be sure to do so after calling this method!
        """
        if not self.file_url:
            return

        request = urllib2.Request(self.file_url)
        request.get_method = lambda: 'HEAD'
        http_file = urllib2.urlopen(request)
        self.file_url_length = http_file.headers['content-length']
        self.file_url_mimetype = http_file.headers['content-type']

    def save_thumbnail(self):
        """
        Automatically run the entire file saving process... provided we have a
        thumbnail_url, that is.
        """
        if not self.thumbnail_url:
            return

        content_thumb = ContentFile(urllib.urlopen(self.thumbnail_url).read())
        self.save_thumbnail_from_file(content_thumb)

    def save_thumbnail_from_file(self, content_thumb):
        """
        Takes an image file-like object and stores it as the thumbnail for this
        video item.
        """
        try:
            pil_image = Image.open(content_thumb)
        except IOError:
            raise CannotOpenImageUrl(
                'An image at the url %s could not be loaded' % (
                    self.thumbnail_url))

        self.thumbnail_extension = pil_image.format.lower()

        # save an unresized version, overwriting if necessary
        default_storage.delete(
            self.get_original_thumb_storage_path())
        default_storage.save(
            self.get_original_thumb_storage_path(),
            content_thumb)

        if hasattr(content_thumb, 'temporary_file_path'):
            # might have gotten moved by Django's storage system, so it might
            # be invalid now.  to make sure we've got a valid file, we reopen
            # under the new path
            content_thumb.close()
            content_thumb = default_storage.open(
                self.get_original_thumb_storage_path())
            pil_image = Image.open(content_thumb)

        # save any resized versions
        self.resize_thumbnail(pil_image)
        self.has_thumbnail = True
        self.save()

    def resize_thumbnail(self, thumb=None):
        """
        Creates resized versions of the video's thumbnail image
        """
        if not thumb:
            thumb = Image.open(
                default_storage.open(self.get_original_thumb_storage_path()))

        for width, height in VIDEO_THUMB_SIZES:
            resized_image = thumb.copy()
            resized_image.thumbnail((width, height), Image.ANTIALIAS)
            if resized_image.size != (width, height):
                x = y = 0
                if resized_image.size[1] < height:
                    y = int((height - resized_image.size[1]) / 2)
                else:
                    x = int((width - resized_image.size[0]) / 2)
                new_image = Image.new('RGBA',
                    (width, height), (0, 0, 0, 0))
                new_image.paste(resized_image, (x, y))
                resized_image = new_image
            sio_img = StringIO.StringIO()
            resized_image.save(sio_img, 'png')
            sio_img.seek(0)
            cf_image = ContentFile(sio_img.read())

            # write file, deleting old thumb if it exists
            default_storage.delete(
                self.get_resized_thumb_storage_path(width, height))
            default_storage.save(
                self.get_resized_thumb_storage_path(width, height),
                cf_image)

    def get_original_thumb_storage_path(self):
        """
        Return the path for the original thumbnail, relative to the default
        file storage system.
        """
        return 'localtv/video_thumbs/%s/orig.%s' % (
            self.id, self.thumbnail_extension)

    def get_resized_thumb_storage_path(self, width, height):
        """
        Return the path for the a thumbnail of a resized width and height,
        relative to the default file storage system.
        """
        return 'localtv/video_thumbs/%s/%sx%s.png' % (
            self.id, width, height)

    def strip_description(self):
        """
        Strip (X)HTML description attributes

        (doesn't run self.save() method though)
        """
        self.description = strip_tags(self.description)

    def submitter(self):
        """
        Return the user that submitted this video.  If necessary, use the
        submitter from the originating feed or savedsearch.
        """
        if self.user is not None:
            return self.user
        elif self.feed is not None:
            return self.feed.user
        elif self.search is not None:
            return self.search.user
        else:
            # XXX warning?
            return None

    def when(self):
        """
        Simple method for gettingeither the when_published date, if available,
        or when_submitted if not
        """

        if self.when_published is not None:
            return self.when_published
        else:
            return self.when_submitted

    @classmethod
    def popular_since(Class, delta, sitelocation=None, **kwargs):
        """
        Returns a QuerySet of the most popular videos in the previous C{delta)
        time.

        @type delta: L{datetime.timedelta)
        @type sitelocation: L{SiteLocation}
        """
        earliest_time = datetime.datetime.now() - delta
        videos = Class.objects.filter(
            watch__timestamp__gte=earliest_time)
        if sitelocation is not None:
            videos = videos.filter(site=sitelocation.site)
        if kwargs:
            videos = videos.filter(**kwargs)
        videos = videos.extra(
            select={'watch__count':
                        """SELECT COUNT(*) FROM localtv_watch
WHERE localtv_video.id = localtv_watch.video_id AND
localtv_watch.timestamp > %s"""},
            select_params = (earliest_time,))
        return videos.order_by('-watch__count').distinct()


class VideoAdmin(admin.ModelAdmin):
    list_display = ('name', 'site', 'when_submitted', 'status', 'feed')
    list_filter = ['status', 'when_submitted']
    search_fields = ['name', 'description']


class Watch(models.Model):
    """
    Record of a video being watched.

    fields:
     - video: Video that was watched
     - timestamp: when watched
     - user: user that watched it, if any
     - ip_address: IP address of the user
    """
    video = models.ForeignKey(Video)
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey('auth.User', blank=True, null=True)
    ip_address = models.IPAddressField()

    @classmethod
    def add(Class, request, video):
        """
        Adds a record of a watched video to the database.  If the request came
        from localhost, check to see if it was forwarded to (hopefully) get the
        right IP address.
        """
        ip = request.META.get('REMOTE_ADDR', '0.0.0.0')
        if ip == '127.0.0.1':
            ip = request.META.get('HTTP_X_FORWARDED_FOR', ip)

        if request.user.is_authenticated():
            user = request.user
        else:
            user = None

        Class(video=video, user=user, ip_address=ip).save()

admin.site.register(OpenIdUser)
admin.site.register(SiteLocation)
admin.site.register(Tag)
admin.site.register(Feed)
admin.site.register(Category, CategoryAdmin)
admin.site.register(Author)
admin.site.register(Video, VideoAdmin)
admin.site.register(SavedSearch)
admin.site.register(Watch)
