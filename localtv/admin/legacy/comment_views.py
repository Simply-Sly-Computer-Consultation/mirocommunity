from django.conf import settings
from django.contrib import comments
from django.contrib.comments import signals
from django.contrib.comments.views import utils
from django import template
from django.shortcuts import render_to_response, get_object_or_404

from localtv.decorators import require_site_admin

#@require_site_admin
def comments_spam(request, comment_id, next=None):
    """
    Mark a comment as spam. Confirmation on GET, action on POST.

    Templates: `comments/spam.html`,
    Context:
        comment
            the spammed `comments.comment` object
    """
    comment = get_object_or_404(comments.get_model(), pk=comment_id, site__pk=settings.SITE_ID)

    # Flag on POST
    if request.method == 'POST':
        flag, created = comments.models.CommentFlag.objects.get_or_create(
            comment = comment,
            user    = request.user,
            flag    = 'spam'
        )

        comment.is_removed = True
        comment.save()

        signals.comment_was_flagged.send(
            sender  = comment.__class__,
            comment = comment,
            flag    = flag,
            created = created,
            request = request,
        )
        return utils.next_redirect(request.POST.copy(), next, spam_done, c=comment.pk)

    # Render a form on GET
    else:
        return render_to_response('comments/spam.html',
            {'comment': comment, "next": next},
            template.RequestContext(request)
        )
comments_spam = require_site_admin(comments_spam)

spam_done = utils.confirmation_view(
    template = "comments/spammed.html",
    doc = 'Displays a "comment was marked as spam" success page.'
)
