from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from internest_core.views import get_user_context

from . import moderation
from .forms import CommentForm, PostForm
from .models import FLAGS_TO_HIDE, HiddenReason, LoungeComment, LoungeFlag, LoungePost, Visibility
from .permissions import founders_only

MAX_DEPTH = 4


def _visible_to(founder):
    return Q(visibility=Visibility.VISIBLE) | Q(author=founder, visibility=Visibility.HIDDEN)


@founders_only
def lounge_feed(request):
    founder = request.founder
    form = PostForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        title, body = form.cleaned_data["title"], form.cleaned_data["body"]
        try:
            moderation.check_post_rate(founder)
            moderation.check_keywords(title, body)
        except moderation.SpamRejected as exc:
            messages.error(request, str(exc))
        else:
            reason = moderation.auto_hide_reason(title, body, moderation.recent_fingerprints())
            post = form.save(commit=False)
            post.author = founder
            post.body_fingerprint = moderation.fingerprint(body)
            if reason:
                post.visibility, post.hidden_reason = Visibility.HIDDEN, reason
            post.save()
            if reason:
                messages.warning(request, _("Your post was received and is waiting for admin review before it appears."))
            else:
                messages.success(request, _("Your post is live."))
            return redirect("lounge_feed")

    tag = request.GET.get("tag", "")
    posts = (
        LoungePost.objects.filter(_visible_to(founder))
        .select_related("author")
        .annotate(
            upvote_count=Count("upvoters", distinct=True),
            comment_count=Count("comments", filter=Q(comments__visibility=Visibility.VISIBLE), distinct=True),
        )
    )
    if tag in dict(LoungePost.CATEGORY_CHOICES):
        posts = posts.filter(category=tag)
    page = Paginator(posts, 20).get_page(request.GET.get("page"))
    upvoted = set(founder.upvoted_lounge_posts.filter(pk__in=[p.pk for p in page]).values_list("pk", flat=True))

    context = get_user_context(request)
    context.update({
        "form": form, "page": page, "upvoted": upvoted, "founder": founder,
        "tags": LoungePost.CATEGORY_CHOICES, "active_tag": tag,
    })
    return render(request, "lounge/feed.html", context)


def _comment_tree(post, founder):
    comments = list(
        post.comments.filter(Q(visibility=Visibility.VISIBLE) | Q(author=founder, visibility=Visibility.HIDDEN))
        .select_related("author")
    )
    by_parent = {}
    for c in comments:
        c.children = []
        by_parent.setdefault(c.parent_id, []).append(c)
    for c in comments:
        c.children = by_parent.get(c.id, [])
    return by_parent.get(None, [])


@founders_only
def post_detail(request, pk):
    founder = request.founder
    post = get_object_or_404(
        LoungePost.objects.select_related("author").annotate(upvote_count=Count("upvoters")),
        Q(pk=pk) & _visible_to(founder),
    )
    context = get_user_context(request)
    context.update({
        "post": post,
        "founder": founder,
        "comments": _comment_tree(post, founder),
        "comment_form": CommentForm(),
        "upvoted": post.upvoters.filter(pk=founder.pk).exists(),
        "flagged": post.flags.filter(reporter=founder).exists(),
        "max_depth": MAX_DEPTH,
    })
    return render(request, "lounge/post_detail.html", context)


def _visible_post_or_404(pk):
    post = LoungePost.objects.filter(pk=pk, visibility=Visibility.VISIBLE).first()
    if post is None:
        raise Http404
    return post


@founders_only
@require_POST
def add_comment(request, pk):
    founder = request.founder
    post = _visible_post_or_404(pk)
    form = CommentForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Please write a comment first."))
        return redirect("lounge_post", pk=post.pk)
    parent = None
    if form.cleaned_data.get("parent_id"):
        parent = get_object_or_404(LoungeComment, pk=form.cleaned_data["parent_id"], post=post)
    body = form.cleaned_data["body"]
    try:
        moderation.check_comment_rate(founder)
        moderation.check_keywords(body)
    except moderation.SpamRejected as exc:
        messages.error(request, str(exc))
        return redirect("lounge_post", pk=post.pk)
    comment = form.save(commit=False)
    comment.post, comment.author, comment.parent = post, founder, parent
    if moderation.SUSPICIOUS_LINK.search(body):
        comment.visibility, comment.hidden_reason = Visibility.HIDDEN, HiddenReason.LINK_SPAM
        messages.warning(request, _("Your comment contains a link that needs admin review before it appears."))
    comment.save()
    return redirect(reverse("lounge_post", args=[post.pk]) + f"#c{comment.pk}")


@founders_only
@require_POST
def toggle_upvote(request, pk):
    post = _visible_post_or_404(pk)
    if post.upvoters.filter(pk=request.founder.pk).exists():
        post.upvoters.remove(request.founder)
    else:
        post.upvoters.add(request.founder)
    nxt = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        nxt = reverse("lounge_post", args=[post.pk])
    return redirect(nxt)


@founders_only
@require_POST
def flag_post(request, pk):
    founder = request.founder
    post = _visible_post_or_404(pk)
    if post.author_id == founder.pk:
        messages.error(request, _("You cannot report your own post."))
        return redirect("lounge_post", pk=post.pk)
    try:
        with transaction.atomic():
            LoungeFlag.objects.create(post=post, reporter=founder)
    except IntegrityError:
        messages.info(request, _("You already reported this post."))
        return redirect("lounge_post", pk=post.pk)
    if post.flags.count() >= FLAGS_TO_HIDE:
        LoungePost.objects.filter(pk=post.pk).update(visibility=Visibility.HIDDEN, hidden_reason=HiddenReason.FLAGS)
        messages.success(request, _("Thanks. The post is now hidden until an admin reviews it."))
        return redirect("lounge_feed")
    messages.success(request, _("Thanks for reporting. Our team will review it."))
    return redirect("lounge_post", pk=post.pk)
