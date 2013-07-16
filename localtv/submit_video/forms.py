import urlparse
import urllib2

from django import forms
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError, NON_FIELD_ERRORS
from django.db.models import Q
from tagging.forms import TagField
import vidscraper
from vidscraper.exceptions import UnhandledVideo

from localtv.models import Video, SiteSettings
from localtv.settings import API_KEYS
from localtv.tasks import video_save_thumbnail
from localtv.templatetags.filters import sanitize


class SubmitURLForm(forms.Form):
    """Accepts submission of a URL."""
    url = forms.URLField(verify_exists=False)

    def _validate_unique(self, url=None, guid=None):
        identifiers = Q()
        if url is not None:
            identifiers |= Q(website_url=url) | Q(file_url=url)
        if guid is not None:
            identifiers |= Q(guid=guid)
        videos = Video.objects.filter(identifiers,
                                      ~Q(status=Video.HIDDEN),
                                      site=Site.objects.get_current())

        # HACK: We set attributes on the form so that we can provide
        # backwards-compatible template context. We should remove this when it's
        # no longer needed.
        try:
            video = videos[0]
        except IndexError:
            self.was_duplicate = False
            self.duplicate_video = None
            self.duplicate_video_pk = None
        else:
            self.was_duplicate = True
            self.duplicate_video_pk = video.pk
            if video.status == Video.PUBLISHED:
                self.duplicate_video = video
            else:
                self.duplicate_video = None
            raise ValidationError("That video has already been submitted!")

    def clean_url(self):
        url = urlparse.urldefrag(self.cleaned_data['url'])[0]
        self._validate_unique(url=url)
        self.video_cache = None
        try:
            self.video_cache = vidscraper.auto_scrape(url, api_keys=API_KEYS)
        except (UnhandledVideo, urllib2.URLError):
            pass
        else:
            if self.video_cache.link is not None and url != self.video_cache.link:
                url = self.video_cache.link
                self._validate_unique(url=url, guid=self.video_cache.guid)
            elif self.video_cache.guid is not None:
                self._validate_unique(guid=self.video_cache.guid)
        return url


class SubmitVideoFormBase(forms.ModelForm):
    tags = TagField(required=False, label="Tags (optional)",
                    help_text=("You may optionally add tags for the video."))

    class Meta:
        exclude = ['status', 'site', 'thumbnail']

    def __init__(self, request, url, *args, **kwargs):
        self.request = request
        site_settings = SiteSettings.objects.get_current()
        super(SubmitVideoFormBase, self).__init__(*args, **kwargs)
        if request.user.is_authenticated():
            self.fields.pop('contact', None)
        elif site_settings.submission_requires_email:
            self.fields['contact'].required = True
            self.fields['contact'].label = 'Email (required)'
        self.instance.site = Site.objects.get_current()
        self.instance.status = Video.NEEDS_MODERATION
        if not self.instance.website_url:
            self.instance.website_url = url

        # HACK for backwards-compatibility
        if 'thumbnail_url' in self.fields:
            self.fields['thumbnail'] = self.fields['thumbnail_url']

    def clean(self):
        cleaned_data = super(SubmitVideoFormBase, self).clean()
        # HACK for backwards-compatibility.
        if 'thumbnail' in cleaned_data:
            thumbnail_url = cleaned_data.pop('thumbnail')
            # prefer thumbnail_url.
            if not cleaned_data.get('thumbnail_url'):
                cleaned_data['thumbnail_url'] = thumbnail_url

        return cleaned_data

    def _post_clean(self):
        super(SubmitVideoFormBase, self)._post_clean()
        # By this time, cleaned data has been applied to the instance.
        identifiers = Q()
        if self.instance.website_url:
            identifiers |= Q(website_url=self.instance.website_url)
        if self.instance.file_url:
            identifiers |= Q(file_url=self.instance.file_url)
        if self.instance.guid:
            identifiers |= Q(guid=self.instance.guid)

        videos = Video.objects.filter(identifiers,
                                      ~Q(status=Video.HIDDEN),
                                      site=Site.objects.get_current())
        if videos.exists():
            self._update_errors({NON_FIELD_ERRORS: ["That video has already "
                                                    "been submitted!"]})

    def clean_description(self):
        return sanitize(self.cleaned_data['description'],
                        extra_filters=['img'])

    def save(self, commit=True):
        instance = super(SubmitVideoFormBase, self).save(commit=False)

        if self.request.user.is_authenticated():
            self.instance.user = self.request.user
            self.instance.contact = self.request.user.email

        if self.request.user_is_admin():
            instance.status = Video.PUBLISHED

        if 'website_url' in self.fields:
            # Then this was a form which required a website_url - i.e. a direct
            # file submission. TODO: Find a better way to mark this?
            instance.try_to_get_file_url_data()

        old_m2m = self.save_m2m

        def save_m2m():
            if instance.status == Video.PUBLISHED:
                # when_submitted isn't set until after the save
                instance.when_approved = instance.when_submitted
                instance.save()
            if hasattr(instance, 'save_m2m'):
                # Then it was generated with from_vidscraper_video
                instance.save_m2m()

            if instance.thumbnail_url and not instance.thumbnail:
                video_save_thumbnail.delay(instance.pk)

            if self.cleaned_data.get('tags'):
                instance.tags = self.cleaned_data['tags']
            old_m2m()
        if commit:
            instance.save()
            save_m2m()
        else:
            self.save_m2m = save_m2m
        return instance


class ThumbnailSubmitVideoForm(SubmitVideoFormBase):
    # For backwards-compatibility.
    thumbnail_file = forms.ImageField(required=False,
                                      label="Thumbnail File (optional)")

    def save(self, commit=True):
        thumbnail = self.cleaned_data.get('thumbnail_file')
        if thumbnail:
            self.instance.thumbnail = thumbnail
            self.instance.thumbnail_url = ''
        return super(ThumbnailSubmitVideoForm, self).save(commit)


class ScrapedSubmitVideoForm(SubmitVideoFormBase):
    pass


class EmbedSubmitVideoForm(ThumbnailSubmitVideoForm):

    def __init__(self, request, url, *args, **kwargs):
        super(EmbedSubmitVideoForm, self).__init__(request, url, *args,
                                                   **kwargs)
        self.fields['embed'] = self.fields['embed_code']

    def clean(self):
        cleaned_data = super(EmbedSubmitVideoForm, self).clean()
        embed_code = cleaned_data.pop('embed')
        # prefer embed_code
        if not cleaned_data.get('embed_code'):
            cleaned_data['embed_code'] = embed_code
        return cleaned_data


class DirectLinkSubmitVideoForm(ThumbnailSubmitVideoForm):

    def __init__(self, request, url, *args, **kwargs):
        super(DirectLinkSubmitVideoForm, self).__init__(request, url, *args,
                                                        **kwargs)

        self.instance.file_url = url
        if self.instance.website_url == url:
            self.instance.website_url = u''

    def save(self, commit=True):
        instance = super(DirectLinkSubmitVideoForm, self).save(commit=False)
        instance.try_to_get_file_url_data()
        if commit:
            instance.save()
            self.save_m2m()
        return instance
