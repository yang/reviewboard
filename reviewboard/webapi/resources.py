import logging
import re
import urllib

import dateutil.parser
from django.conf import settings
from django.contrib import auth
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied, ObjectDoesNotExist
from django.db.models import Q
from django.http import HttpResponseRedirect, HttpResponse
from django.template.defaultfilters import timesince
from django.utils.translation import ugettext as _
from djblets.siteconfig.models import SiteConfiguration
from djblets.util.decorators import augment_method_from
from djblets.util.http import get_http_requested_mimetype, \
                              set_last_modified
from djblets.webapi.core import WebAPIResponseFormError, \
                                WebAPIResponsePaginated, \
                                WebAPIResponse
from djblets.webapi.decorators import webapi_login_required, \
                                      webapi_response_errors, \
                                      webapi_request_fields
from djblets.webapi.errors import DOES_NOT_EXIST, INVALID_FORM_DATA, \
                                  PERMISSION_DENIED
from djblets.webapi.resources import WebAPIResource as DjbletsWebAPIResource, \
                                     UserResource as DjbletsUserResource, \
                                     RootResource as DjbletsRootResource, \
                                     register_resource_for_model, \
                                     get_resource_for_object

from reviewboard import get_version_string, get_package_version, is_release
from reviewboard.accounts.models import Profile
from reviewboard.diffviewer.diffutils import get_diff_files
from reviewboard.diffviewer.forms import EmptyDiffError
from reviewboard.reviews.errors import PermissionError
from reviewboard.reviews.forms import UploadDiffForm, UploadScreenshotForm
from reviewboard.reviews.models import Comment, DiffSet, FileDiff, Group, \
                                       Repository, ReviewRequest, \
                                       ReviewRequestDraft, Review, \
                                       ScreenshotComment, Screenshot
from reviewboard.scmtools.errors import ChangeNumberInUseError, \
                                        EmptyChangeSetError, \
                                        FileNotFoundError, \
                                        InvalidChangeNumberError
from reviewboard.webapi.decorators import webapi_check_login_required
from reviewboard.webapi.errors import CHANGE_NUMBER_IN_USE, \
                                      EMPTY_CHANGESET, \
                                      INVALID_CHANGE_NUMBER, \
                                      INVALID_REPOSITORY, \
                                      INVALID_USER, \
                                      REPO_FILE_NOT_FOUND, \
                                      REPO_INFO_ERROR, \
                                      REPO_NOT_IMPLEMENTED


CUSTOM_MIMETYPE_BASE = 'application/vnd.reviewboard.org'


class WebAPIResource(DjbletsWebAPIResource):
    """A specialization of the Djblets WebAPIResource for Review Board."""

    @webapi_check_login_required
    @augment_method_from(DjbletsWebAPIResource)
    def get(self, *args, **kwargs):
        """Returns the serialized object for the resource.

        This will require login if anonymous access isn't enabled on the
        site.
        """
        pass

    @webapi_check_login_required
    @webapi_request_fields(
        optional=dict({
            'counts-only': {
                'type': bool,
                'description': 'If specified, a single ``count`` field is '
                               'returned with the number of results, instead '
                               'of the results themselves.',
            },
        }, **DjbletsWebAPIResource.get_list.optional_fields),
        required=DjbletsWebAPIResource.get_list.required_fields,
        allow_unknown=True
    )
    def get_list(self, request, *args, **kwargs):
        """Returns a list of objects.

        This will require login if anonymous access isn't enabled on the
        site.

        If ``?counts-only=1`` is passed on the URL, then this will return
        only a ``count`` field with the number of entries, instead of the
        serialized objects.
        """
        if self.model and request.GET.get('counts-only', False):
            return 200, {
                'count': self.get_queryset(request, is_list=True,
                                           *args, **kwargs).count()
            }
        else:
            return self._get_list_impl(request, *args, **kwargs)

    def _get_list_impl(self, request, *args, **kwargs):
        """Actual implementation to return the list of results.

        This by default calls the parent WebAPIResource.get_list, but this
        can be overridden by subclasses to provide a more custom
        implementation while still retaining the ?counts-only=1 functionality.
        """
        return super(WebAPIResource, self).get_list(request, *args, **kwargs)


class BaseDiffCommentResource(WebAPIResource):
    """Base class for diff comment resources.

    Provides common fields and functionality for all diff comment resources.
    """
    model = Comment
    name = 'diff_comment'
    fields = {
        'id': {
            'type': int,
            'description': 'The numeric ID of the comment.',
        },
        'first_line': {
            'type': int,
            'description': 'The line number that the comment starts at.',
        },
        'num_lines': {
            'type': int,
            'description': 'The number of lines the comment spans.',
        },
        'text': {
            'type': str,
            'description': 'The comment text.',
        },
        'filediff': {
            'type': 'reviewboard.webapi.resources.FileDiffResource',
            'description': 'The per-file diff that the comment was made on.',
        },
        'interfilediff': {
            'type': 'reviewboard.webapi.resources.FileDiffResource',
            'description': "The second per-file diff in an interdiff that "
                           "the comment was made on. This will be ``null`` if "
                           "the comment wasn't made on an interdiff.",
        },
        'timestamp': {
            'type': str,
            'description': 'The date and time that the comment was made '
                           '(in YYYY-MM-DD HH:MM:SS format).',
        },
        'public': {
            'type': bool,
            'description': 'Whether or not the comment is part of a public '
                           'review.',
        },
        'user': {
            'type': 'reviewboard.webapi.resources.UserResource',
            'description': 'The user who made the comment.',
        },
    }

    uri_object_key = 'comment_id'

    allowed_methods = ('GET',)

    def get_queryset(self, request, review_request_id, is_list=False,
                     *args, **kwargs):
        """Returns a queryset for Comment models.

        This filters the query for comments on the specified review request
        which are either public or owned by the requesting user.

        If the queryset is being used for a list of comment resources,
        then this can be further filtered by passing ``?interdiff-revision=``
        on the URL to match the given interdiff revision, and
        ``?line=`` to match comments on the given line number.
        """
        q = self.model.objects.filter(
            Q(review__public=True) | Q(review__user=request.user),
            filediff__diffset__history__review_request=review_request_id)

        if is_list:
            if 'interdiff-revision' in request.GET:
                interdiff_revision = int(request.GET['interdiff-revision'])
                q = q.filter(
                    interfilediff__diffset__revision=interdiff_revision)

            if 'line' in request.GET:
                q = q.filter(first_line=int(request.GET['line']))

        return q

    def serialize_public_field(self, obj):
        return obj.review.get().public

    def serialize_timesince_field(self, obj):
        return timesince(obj.timestamp)

    def serialize_user_field(self, obj):
        return obj.review.get().user

    @webapi_request_fields(optional={
        'interdiff-revision': {
            'type': int,
            'description': 'The second revision in an interdiff revision '
                           'range. The comments will be limited to this range.',
        },
        'line': {
            'type': int,
            'description': 'The line number that each comment must start on.',
        },
    })
    @augment_method_from(WebAPIResource)
    def get_list(self, *args, **kwargs):
        pass

    @augment_method_from(WebAPIResource)
    def get(self, *args, **kwargs):
        """Returns information on the comment."""
        pass


class FileDiffCommentResource(BaseDiffCommentResource):
    """Provides information on comments made on a particular per-file diff.

    The list of comments cannot be modified from this resource. It's meant
    purely as a way to see existing comments that were made on a diff. These
    comments will span all public reviews.
    """
    allowed_methods = ('GET',)
    model_parent_key = 'filediff'
    uri_object_key = None

    def get_queryset(self, request, review_request_id, diff_revision,
                     *args, **kwargs):
        """Returns a queryset for Comment models.

        This filters the query for comments on the specified review request
        and made on the specified diff revision, which are either public or
        owned by the requesting user.

        If the queryset is being used for a list of comment resources,
        then this can be further filtered by passing ``?interdiff-revision=``
        on the URL to match the given interdiff revision, and
        ``?line=`` to match comments on the given line number.
        """
        q = super(FileDiffCommentResource, self).get_queryset(
            request, review_request_id, *args, **kwargs)
        return q.filter(filediff__diffset__revision=diff_revision)

    @augment_method_from(BaseDiffCommentResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of comments on a file in a diff.

        This list can be filtered down by using the ``?line=`` and
        ``?interdiff-revision=``.

        To filter for comments that start on a particular line in the file,
        using ``?line=``.

        To filter for comments that span revisions of diffs, you can specify
        the second revision in the range using ``?interdiff-revision=``.
        """
        pass

filediff_comment_resource = FileDiffCommentResource()


class ReviewDiffCommentResource(BaseDiffCommentResource):
    """Provides information on diff comments made on a review.

    If the review is a draft, then comments can be added, deleted, or
    changed on this list. However, if the review is already published,
    then no changes can be made.
    """
    allowed_methods = ('GET', 'POST', 'PUT', 'DELETE')
    model_parent_key = 'review'

    def get_queryset(self, request, review_request_id, review_id,
                     *args, **kwargs):
        q = super(ReviewDiffCommentResource, self).get_queryset(
            request, review_request_id, *args, **kwargs)
        return q.filter(review=review_id)

    def has_delete_permissions(self, request, comment, *args, **kwargs):
        review = comment.review.get()
        return not review.public and review.user == request.user

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, INVALID_FORM_DATA,
                            PERMISSION_DENIED)
    @webapi_request_fields(
        required = {
            'filediff_id': {
                'type': int,
                'description': 'The ID of the file diff the comment is on.',
            },
            'first_line': {
                'type': int,
                'description': 'The line number the comment starts at.',
            },
            'num_lines': {
                'type': int,
                'description': 'The number of lines the comment spans.',
            },
            'text': {
                'type': str,
                'description': 'The comment text.',
            },
        },
        optional = {
            'interfilediff_id': {
                'type': int,
                'description': 'The ID of the second file diff in the '
                               'interdiff the comment is on.',
            },
        },
    )
    def create(self, request, first_line, num_lines, text,
               filediff_id, interfilediff_id=None, *args, **kwargs):
        """Creates a new diff comment.

        This will create a new diff comment on this review. The review
        must be a draft review.
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
            review = review_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_resource.has_modify_permissions(request, review):
            return PERMISSION_DENIED

        filediff = None
        interfilediff = None
        invalid_fields = {}

        try:
            filediff = FileDiff.objects.get(
                pk=filediff_id,
                diffset__history__review_request=review_request)
        except ObjectDoesNotExist:
            invalid_fields['filediff_id'] = \
                ['This is not a valid filediff ID']

        if filediff and interfilediff_id:
            if interfilediff_id == filediff.id:
                invalid_fields['interfilediff_id'] = \
                    ['This cannot be the same as filediff_id']
            else:
                try:
                    interfilediff = FileDiff.objects.get(
                        pk=interfilediff_id,
                        diffset__history=filediff.diffset.history)
                except ObjectDoesNotExist:
                    invalid_fields['interfilediff_id'] = \
                        ['This is not a valid interfilediff ID']

        if invalid_fields:
            return INVALID_FORM_DATA, {
                'fields': invalid_fields,
            }

        new_comment = self.model(filediff=filediff,
                                 interfilediff=interfilediff,
                                 text=text,
                                 first_line=first_line,
                                 num_lines=num_lines)
        new_comment.save()

        review.comments.add(new_comment)
        review.save()

        return 201, {
            self.item_result_key: new_comment,
        }

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(
        optional = {
            'first_line': {
                'type': int,
                'description': 'The line number the comment starts at.',
            },
            'num_lines': {
                'type': int,
                'description': 'The number of lines the comment spans.',
            },
            'text': {
                'type': str,
                'description': 'The comment text.',
            },
        },
    )
    def update(self, request, *args, **kwargs):
        """Updates a diff comment.

        This can update the text or line range of an existing comment.
        """
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            review = review_resource.get_object(request, *args, **kwargs)
            diff_comment = self.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_resource.has_modify_permissions(request, review):
            return PERMISSION_DENIED

        for field in ('text', 'first_line', 'num_lines'):
            value = kwargs.get(field, None)

            if value is not None:
                setattr(diff_comment, field, value)

        diff_comment.save()

        return 200, {
            self.item_result_key: diff_comment,
        }

    @augment_method_from(BaseDiffCommentResource)
    def delete(self, *args, **kwargs):
        """Deletes the comment.

        This will remove the comment from the review. This cannot be undone.

        Only comments on draft reviews can be deleted. Attempting to delete
        a published comment will return a Permission Denied error.

        Instead of a payload response, this will return :http:`204`.
        """
        pass

    @augment_method_from(BaseDiffCommentResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of comments made on a review.

        This list can be filtered down by using the ``?line=`` and
        ``?interdiff-revision=``.

        To filter for comments that start on a particular line in the file,
        using ``?line=``.

        To filter for comments that span revisions of diffs, you can specify
        the second revision in the range using ``?interdiff-revision=``.
        """
        pass

review_diff_comment_resource = ReviewDiffCommentResource()


class ReviewReplyDiffCommentResource(BaseDiffCommentResource):
    """Provides information on replies to diff comments made on a review reply.

    If the reply is a draft, then comments can be added, deleted, or
    changed on this list. However, if the reply is already published,
    then no changed can be made.
    """
    allowed_methods = ('GET', 'POST', 'PUT', 'DELETE')
    model_parent_key = 'review'
    fields = dict({
        'reply_to': {
            'type': ReviewDiffCommentResource,
            'description': 'The comment being replied to.',
        },
    }, **BaseDiffCommentResource.fields)

    def get_queryset(self, request, review_request_id, review_id, reply_id,
                     *args, **kwargs):
        q = super(ReviewReplyDiffCommentResource, self).get_queryset(
            request, review_request_id, *args, **kwargs)
        q = q.filter(review=reply_id, review__base_reply_to=review_id)
        return q

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, INVALID_FORM_DATA,
                            PERMISSION_DENIED)
    @webapi_request_fields(
        required = {
            'reply_to_id': {
                'type': int,
                'description': 'The ID of the comment being replied to.',
            },
            'text': {
                'type': str,
                'description': 'The comment text.',
            },
        },
    )
    def create(self, request, reply_to_id, text, *args, **kwargs):
        """Creates a new reply to a diff comment on the parent review.

        This will create a new diff comment as part of this reply. The reply
        must be a draft reply.
        """
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            reply = review_reply_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_reply_resource.has_modify_permissions(request, reply):
            return PERMISSION_DENIED

        try:
            comment = \
                review_diff_comment_resource.get_object(request,
                                                        comment_id=reply_to_id,
                                                        *args, **kwargs)
        except ObjectDoesNotExist:
            return INVALID_FORM_DATA, {
                'fields': {
                    'reply_to_id': ['This is not a valid comment ID'],
                }
            }

        new_comment = self.model(filediff=comment.filediff,
                                 interfilediff=comment.interfilediff,
                                 reply_to=comment,
                                 text=text,
                                 first_line=comment.first_line,
                                 num_lines=comment.num_lines)
        new_comment.save()

        reply.comments.add(new_comment)
        reply.save()

        return 201, {
            self.item_result_key: new_comment,
        }

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(
        required = {
            'text': {
                'type': str,
                'description': 'The new comment text.',
            },
        },
    )
    def update(self, request, *args, **kwargs):
        """Updates a reply to a diff comment.

        This can only update the text in the comment. The comment being
        replied to cannot change.
        """
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            reply = review_reply_resource.get_object(request, *args, **kwargs)
            diff_comment = self.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_reply_resource.has_modify_permissions(request, reply):
            return PERMISSION_DENIED

        for field in ('text',):
            value = kwargs.get(field, None)

            if value is not None:
                setattr(diff_comment, field, value)

        diff_comment.save()

        return 200, {
            self.item_result_key: diff_comment,
        }

    @augment_method_from(BaseDiffCommentResource)
    def delete(self, *args, **kwargs):
        """Deletes a comment from a draft reply.

        This will remove the comment from the reply. This cannot be undone.

        Only comments on draft replies can be deleted. Attempting to delete
        a published comment will return a Permission Denied error.

        Instead of a payload response, this will return :http:`204`.
        """
        pass

    @augment_method_from(BaseDiffCommentResource)
    def get(self, *args, **kwargs):
        """Returns information on a reply to a comment.

        Much of the information will be identical to that of the comment
        being replied to. For example, the range of lines. This is because
        the reply to the comment is meant to cover the exact same code that
        the original comment covers.
        """
        pass

    @augment_method_from(BaseDiffCommentResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of replies to comments made on a review reply.

        This list can be filtered down by using the ``?line=`` and
        ``?interdiff-revision=``.

        To filter for comments that start on a particular line in the file,
        using ``?line=``.

        To filter for comments that span revisions of diffs, you can specify
        the second revision in the range using ``?interdiff-revision=``.
        """
        pass

review_reply_diff_comment_resource = ReviewReplyDiffCommentResource()


class FileDiffResource(WebAPIResource):
    """Provides information on per-file diffs.

    Each of these contains a single, self-contained diff file that
    applies to exactly one file on a repository.
    """
    model = FileDiff
    name = 'file'
    fields = {
        'id': {
            'type': int,
            'description': 'The numeric ID of the file diff.',
        },
        'source_file': {
            'type': str,
            'description': 'The original name of the modified file in the '
                           'diff.',
        },
        'dest_file': {
            'type': str,
            'description': 'The new name of the patched file. This may be '
                           'the same as the existing file.',
        },
        'source_revision': {
            'type': str,
            'description': 'The revision of the file being modified. This '
                           'is a valid revision in the repository.',
        },
        'dest_detail': {
            'type': str,
            'description': 'Additional information of the destination file. '
                           'This is parsed from the diff, but is usually '
                           'not used for anything.',
        },
    }
    item_child_resources = [filediff_comment_resource]

    uri_object_key = 'filediff_id'
    model_parent_key = 'diffset'

    DIFF_DATA_MIMETYPE_BASE = CUSTOM_MIMETYPE_BASE + '.diff.data'
    DIFF_DATA_MIMETYPE_JSON = DIFF_DATA_MIMETYPE_BASE + '+json'
    DIFF_DATA_MIMETYPE_XML = DIFF_DATA_MIMETYPE_BASE + '+xml'

    allowed_item_mimetypes = WebAPIResource.allowed_item_mimetypes + [
        'text/x-patch',
        DIFF_DATA_MIMETYPE_JSON,
        DIFF_DATA_MIMETYPE_XML,
    ]

    def get_queryset(self, request, review_request_id, diff_revision,
                     *args, **kwargs):
        return self.model.objects.filter(
            diffset__history__review_request=review_request_id,
            diffset__revision=diff_revision)

    @augment_method_from(WebAPIResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of public per-file diffs on the review request.

        Each per-file diff has information about the diff. It does not
        provide the contents of the diff. For that, access the per-file diff's
        resource directly and use the correct mimetype.
        """
        pass

    @webapi_check_login_required
    def get(self, request, *args, **kwargs):
        """Returns the information or contents on a per-file diff.

        The output varies by mimetype.

        If :mimetype:`application/json` or :mimetype:`application/xml` is
        used, then the fields for the diff are returned, like with any other
        resource.

        If :mimetype:`text/x-patch` is used, then the actual diff file itself
        is returned. This diff should be as it was when uploaded originally,
        for this file only, with potentially some extra SCM-specific headers
        stripped.

        If :mimetype:`application/vnd.reviewboard.org.diff.data+json` or
        :mimetype:`application/vnd.reviewboard.org.diff.data+xml` is used,
        then the raw diff data (lists of inserts, deletes, replaces, moves,
        header information, etc.) is returned in either JSON or XML. This
        contains nearly all of the information used to render the diff in
        the diff viewer, and can be useful for building a diff viewer that
        interfaces with Review Board.

        If ``?syntax-highlighting=1`` is passed, the rendered diff content
        for each line will contain HTML markup showing syntax highlighting.
        Otherwise, the content will be in plain text.

        The format of the diff data is a bit complex. The data is stored
        under a top-level ``diff_data`` element and contains the following
        information:

        .. list-table::
           :header-rows: 1
           :widths: 25 15 60

           * - Field
             - Type
             - Description

           * - **binary**
             - Boolean
             - Whether or not the file is a binary file. Binary files
               won't have any diff content to display.

           * - **chunks**
             - List of Dictionary
             - A list of chunks. These are used to render the diff. See below.

           * - **changed_chunk_indexes**
             - List of Integer
             - The list of chunks in the diff that have actual changes
               (inserts, deletes, or replaces).

           * - **new_file**
             - Boolean
             - Whether or not this is a newly added file, rather than an
               existing file in the repository.

           * - **num_changes**
             - Integer
             - The number of changes made in this file (chunks of adds,
               removes, or deletes).

        Each chunk contains the following fields:

        .. list-table::
           :header-rows: 1
           :widths: 25 15 60

           * - Field
             - Type
             - Description

           * - **change**
             - One of ``equal``, ``delete``, ``insert``, ``replace``
             - The type of change on this chunk. The type influences what
               sort of information is available for the chunk.

           * - **collapsable**
             - Boolean
             - Whether or not this chunk is collapseable. A collapseable chunk
               is one that is hidden by default in the diff viewer, but can
               be expanded. These will always be ``equal`` chunks, but not
               every ``equal`` chunk is necessarily collapseable (as they
               may be there to provide surrounding context for the changes).

           * - **index**
             - Integer
             - The index of the chunk. This is 0-based.

           * - **lines**
             - List of List
             - The list of rendered lines for a side-by-side diff. Each
               entry in the list is itself a list with 8 items:

               1. Row number of the line in the combined side-by-side diff.
               2. The line number of the line in the left-hand file, as an
                  integer (for ``replace``, ``delete``, and ``equal`` chunks)
                  or an empty string (for ``insert``).
               3. The text for the line in the left-hand file.
               4. The indexes within the text for the left-hand file that
                  have been replaced by text in the right-hand side. Each
                  index is a list of ``start, end`` positions, 0-based.
                  This is only available for ``replace`` lines. Otherwise the
                  list is empty.
               5. The line number of the line in the right-hand file, as an
                  integer (for ``replace``, ``insert`` and ``equal`` chunks)
                  or an empty string (for ``delete``).
               6. The text for the line in the right-hand file.
               7. The indexes within the text for the right-hand file that
                  are replacements for text in the left-hand file. Each
                  index is a list of ``start, end`` positions, 0-based.
                  This is only available for ``replace`` lines. Otherwise the
                  list is empty.
               8. A boolean that indicates if the line contains only
                  whitespace changes.

           * - **meta**
             - Dictionary
             - Additional information about the chunk. See below for more
               information.

           * - **numlines**
             - Integer
             - The number of lines in the chunk.

        A chunk's meta information contains:

        .. list-table::
           :header-rows: 1
           :widths: 25 15 60

           * - Field
             - Type
             - Description

           * - **headers**
             - List of (String, String)
             - Class definitions, function definitions, or other useful
               headers that should be displayed before this chunk. This helps
               users to identify where in a file they are and what the current
               chunk may be a part of.

           * - **whitespace_chunk**
             - Boolean
             - Whether or not the entire chunk consists only of whitespace
               changes.

           * - **whitespace_lines**
             - List of (Integer, Integer)
             - A list of ``start, end`` row indexes in the lins that contain
               whitespace-only changes. These are 1-based.

        Other meta information may be available, but most is intended for
        internal use and shouldn't be relied upon.
        """
        mimetype = get_http_requested_mimetype(request,
                                               self.allowed_item_mimetypes)

        if mimetype == 'text/x-patch':
            return self._get_patch(request, *args, **kwargs)
        elif mimetype.startswith(self.DIFF_DATA_MIMETYPE_BASE + "+"):
            return self._get_diff_data(request, mimetype, *args, **kwargs)
        else:
            return super(FileDiffResource, self).get(request, *args, **kwargs)

    def _get_patch(self, request, *args, **kwargs):
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            filediff = self.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        resp = HttpResponse(filediff.diff, mimetype='text/x-patch')
        filename = '%s.patch' % urllib.quote(filediff.source_file)
        resp['Content-Disposition'] = 'inline; filename=%s' % filename
        set_last_modified(resp, filediff.diffset.timestamp)

        return resp

    def _get_diff_data(self, request, mimetype, *args, **kwargs):
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            filediff = self.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        highlighting = request.GET.get('syntax-highlighting', False)

        files = get_diff_files(filediff.diffset, filediff,
                               enable_syntax_highlighting=highlighting)

        if not files:
            # This may not be the right error here.
            return DOES_NOT_EXIST

        assert len(files) == 1
        f = files[0]

        payload = {
            'diff_data': {
                'binary': f['binary'],
                'chunks': f['chunks'],
                'num_changes': f['num_changes'],
                'changed_chunk_indexes': f['changed_chunk_indexes'],
                'new_file': f['newfile'],
            }
        }

        # XXX: Kind of a hack.
        api_format = mimetype.split('+')[-1]

        resp = WebAPIResponse(request, payload, api_format=api_format)
        set_last_modified(resp, filediff.diffset.timestamp)

        return resp

filediff_resource = FileDiffResource()


class DiffResource(WebAPIResource):
    """Provides information on a collection of complete diffs.

    Each diff contains individual per-file diffs as child resources.
    A diff is revisioned, and more than one can be associated with any
    particular review request.
    """
    model = DiffSet
    name = 'diff'
    fields = {
        'id': {
            'type': int,
            'description': 'The numeric ID of the diff.',
        },
        'name': {
            'type': str,
            'description': 'The name of the diff, usually the filename.',
        },
        'revision': {
            'type': int,
            'description': 'The revision of the diff. Starts at 1 for public '
                           'diffs. Draft diffs may be at 0.',
        },
        'timestamp': {
            'type': str,
            'description': 'The date and time that the diff was uploaded '
                           '(in YYYY-MM-DD HH:MM:SS format).',
        },
        'repository': {
            'type': 'reviewboard.webapi.resources.RepositoryResource',
            'description': 'The repository that the diff is applied against.',
        },
    }
    item_child_resources = [filediff_resource]

    allowed_methods = ('GET', 'POST')

    uri_object_key = 'diff_revision'
    model_object_key = 'revision'
    model_parent_key = 'history'

    allowed_mimetypes = [
        'application/json',
        'application/xml',
        'text/x-patch'
    ]

    def get_queryset(self, request, review_request_id, *args, **kwargs):
        return self.model.objects.filter(
            history__review_request=review_request_id)

    def get_parent_object(self, diffset):
        history = diffset.history

        if history:
            return history.review_request.get()
        else:
            # This isn't in a history yet. It's part of a draft.
            return diffset.review_request_draft.get().review_request

    def has_access_permissions(self, request, diffset, *args, **kwargs):
        review_request = diffset.history.review_request.get()
        return review_request.is_accessible_by(request.user)

    @augment_method_from(WebAPIResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of public diffs on the review request.

        Each diff has a revision and list of per-file diffs associated with it.
        """
        pass

    @webapi_check_login_required
    def get(self, request, *args, **kwargs):
        """Returns the information or contents on a particular diff.

        The output varies by mimetype.

        If :mimetype:`application/json` or :mimetype:`application/xml` is
        used, then the fields for the diff are returned, like with any other
        resource.

        If :mimetype:`text/x-patch` is used, then the actual diff file itself
        is returned. This diff should be as it was when uploaded originally,
        with potentially some extra SCM-specific headers stripped. The
        contents will contain that of all per-file diffs that make up this
        diff.
        """
        mimetype = get_http_requested_mimetype(request,
                                               self.allowed_mimetypes)

        if mimetype == 'text/x-patch':
            return self._get_patch(request, *args, **kwargs)
        else:
            return super(DiffResource, self).get(request, *args, **kwargs)

    def _get_patch(self, request, *args, **kwargs):
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
            diffset = self.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        tool = review_request.repository.get_scmtool()
        data = tool.get_parser('').raw_diff(diffset)

        resp = HttpResponse(data, mimetype='text/x-patch')

        if diffset.name == 'diff':
            filename = 'bug%s.patch' % \
                       review_request.bugs_closed.replace(',', '_')
        else:
            filename = diffset.name

        resp['Content-Disposition'] = 'inline; filename=%s' % filename
        set_last_modified(resp, diffset.timestamp)

        return resp

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED,
                            REPO_FILE_NOT_FOUND, INVALID_FORM_DATA)
    @webapi_request_fields(
        required={
            'path': {
                'type': file,
                'description': 'The main diff to upload.',
            },
        },
        optional={
            'basedir': {
                'type': str,
                'description': 'The base directory that will prepended to '
                               'all paths in the diff. This is needed for '
                               'some types of repositories. The directory '
                               'must be between the root of the repository '
                               'and the top directory referenced in the '
                               'diff paths.',
            },
            'parent_diff_path': {
                'type': file,
                'description': 'The optional parent diff to upload.',
            },
        }
    )
    def create(self, request, *args, **kwargs):
        """Creates a new diff by parsing an uploaded diff file.

        This will implicitly create the new Review Request draft, which can
        be updated separately and then published.

        This accepts a unified diff file, validates it, and stores it along
        with the draft of a review request. The new diff will have a revision
        of 0.

        A parent diff can be uploaded along with the main diff. A parent diff
        is a diff based on an existing commit in the repository, which will
        be applied before the main diff. The parent diff will not be included
        in the diff viewer. It's useful when developing a change based on a
        branch that is not yet committed. In this case, a parent diff of the
        parent branch would be provided along with the diff of the new commit,
        and only the new commit will be shown.

        It is expected that the client will send the data as part of a
        :mimetype:`multipart/form-data` mimetype. The main diff's name and
        content would be stored in the ``path`` field. If a parent diff is
        provided, its name and content would be stored in the
        ``parent_diff_path`` field.

        An example of this would be::

            -- SoMe BoUnDaRy
            Content-Disposition: form-data; name=path; filename="foo.diff"

            <Unified Diff Content Here>
            -- SoMe BoUnDaRy --
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
        except ReviewRequest.DoesNotExist:
            return DOES_NOT_EXIST

        if not review_request.is_mutable_by(request.user):
            return PERMISSION_DENIED

        form_data = request.POST.copy()
        form = UploadDiffForm(review_request, form_data, request.FILES)

        if not form.is_valid():
            return WebAPIResponseFormError(request, form)

        try:
            diffset = form.create(request.FILES['path'],
                                  request.FILES.get('parent_diff_path'))
        except FileNotFoundError, e:
            return REPO_FILE_NOT_FOUND, {
                'file': e.path,
                'revision': e.revision
            }
        except EmptyDiffError, e:
            return INVALID_FORM_DATA, {
                'fields': {
                    'path': [str(e)]
                }
            }
        except Exception, e:
            # This could be very wrong, but at least they'll see the error.
            # We probably want a new error type for this.
            logging.error("Error uploading new diff: %s", e, exc_info=1)

            return INVALID_FORM_DATA, {
                'fields': {
                    'path': [str(e)]
                }
            }

        discarded_diffset = None

        try:
            draft = review_request.draft.get()

            if draft.diffset and draft.diffset != diffset:
                discarded_diffset = draft.diffset
        except ReviewRequestDraft.DoesNotExist:
            try:
                draft = ReviewRequestDraftResource.prepare_draft(
                    request, review_request)
            except PermissionDenied:
                return PERMISSION_DENIED

        draft.diffset = diffset

        # We only want to add default reviewers the first time.  Was bug 318.
        if review_request.diffset_history.diffsets.count() == 0:
            draft.add_default_reviewers();

        draft.save()

        if discarded_diffset:
            discarded_diffset.delete()

        # E-mail gets sent when the draft is saved.

        return 201, {
            self.item_result_key: diffset,
        }

diffset_resource = DiffResource()


class BaseWatchedObjectResource(WebAPIResource):
    """A base resource for objects watched by a user."""
    watched_resource = None
    uri_object_key = 'watched_obj_id'
    profile_field = None
    star_function = None
    unstar_function = None

    allowed_methods = ('GET', 'POST', 'DELETE')

    @property
    def uri_object_key_regex(self):
        return self.watched_resource.uri_object_key_regex

    def get_queryset(self, request, username, *args, **kwargs):
        try:
            profile = Profile.objects.get(user__username=username)
            q = self.watched_resource.get_queryset(request, *args, **kwargs)
            q = q.filter(starred_by=profile)
            return q
        except Profile.DoesNotExist:
            return self.watched_resource.model.objects.none()

    @webapi_check_login_required
    def get(self, request, watched_obj_id, *args, **kwargs):
        try:
            q = self.get_queryset(request, *args, **kwargs)
            obj = q.get(pk=watched_obj_id)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        return HttpResponseRedirect(
            self.watched_resource.get_href(obj, request, *args, **kwargs))

    @webapi_check_login_required
    def get_list(self, request, *args, **kwargs):
        # TODO: Handle pagination and ?counts-only=1
        objects = [
            self.serialize_object(obj)
            for obj in self.get_queryset(request, is_list=True,
                                         *args, **kwargs)
        ]

        return 200, {
            self.list_result_key: objects,
        }

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(required={
        'object_id': {
            'type': str,
            'description': 'The ID of the object to watch.',
        },
    })
    def create(self, request, object_id, *args, **kwargs):
        try:
            obj = self.watched_resource.get_object(request, **dict({
                self.watched_resource.uri_object_key: object_id,
            }))
            user = user_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not user_resource.has_modify_permissions(request, user,
                                                    *args, **kwargs):
            return PERMISSION_DENIED

        profile, profile_is_new = \
            Profile.objects.get_or_create(user=request.user)
        star = getattr(profile, self.star_function)
        star(obj)

        return 201, {
            self.item_result_key: obj,
        }

    @webapi_login_required
    def delete(self, request, watched_obj_id, *args, **kwargs):
        try:
            obj = self.watched_resource.get_object(request, **dict({
                self.watched_resource.uri_object_key: watched_obj_id,
            }))
            user = user_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not user_resource.has_modify_permissions(request, user,
                                                   *args, **kwargs):
            return PERMISSION_DENIED

        profile, profile_is_new = \
            Profile.objects.get_or_create(user=request.user)

        if not profile_is_new:
            unstar = getattr(profile, self.unstar_function)
            unstar(obj)

        return 204, {}

    def serialize_object(self, obj, *args, **kwargs):
        return {
            'id': obj.pk,
            self.item_result_key: obj,
        }


class WatchedReviewGroupResource(BaseWatchedObjectResource):
    """Lists and manipulates entries for review groups watched by the user.

    These are groups that the user has starred in their Dashboard.
    This resource can be used for listing existing review groups and adding
    new review groups to watch.

    Each item in the resource is an association between the user and the
    review group. The entries in the list are not the review groups themselves,
    but rather an entry that represents this association by listing the
    association's ID (which can be used for removing the association) and
    linking to the review group.
    """
    name = 'watched_review_group'
    uri_name = 'review-groups'
    profile_field = 'starred_groups'
    star_function = 'star_review_group'
    unstar_function = 'unstar_review_group'

    @property
    def watched_resource(self):
        """Return the watched resource.

        This is implemented as a property in order to work around
        a circular reference issue.
        """
        return review_group_resource

    @augment_method_from(BaseWatchedObjectResource)
    def get(self, *args, **kwargs):
        """Returned an :http:`302` pointing to the review group being
        watched.

        Rather than returning a body with the entry, performing an HTTP GET
        on this resource will redirect the client to the actual review group
        being watched.

        Clients must properly handle :http:`302` and expect this redirect
        to happen.
        """
        pass

    @augment_method_from(BaseWatchedObjectResource)
    def get_list(self, *args, **kwargs):
        """Retrieves the list of watched review groups.

        Each entry in the list consists of a numeric ID that represents the
        entry for the watched review group. This is not necessarily the ID
        of the review group itself. It's used for looking up the resource
        of the watched item so that it can be removed.
        """
        pass

    @augment_method_from(BaseWatchedObjectResource)
    def create(self, *args, **kwargs):
        """Marks a review group as being watched.

        The ID of the review group must be passed as ``object_id``, and will
        store that review group in the list.
        """
        pass

    @augment_method_from(BaseWatchedObjectResource)
    def delete(self, *args, **kwargs):
        """Deletes a watched review group entry.

        This is the same effect as unstarring a review group. It does
        not actually delete the review group, just the entry in the list.
        """
        pass

watched_review_group_resource = WatchedReviewGroupResource()


class WatchedReviewRequestResource(BaseWatchedObjectResource):
    """Lists and manipulates entries for review requests watched by the user.

    These are requests that the user has starred in their Dashboard.
    This resource can be used for listing existing review requests and adding
    new review requests to watch.

    Each item in the resource is an association between the user and the
    review request. The entries in the list are not the review requests
    themselves, but rather an entry that represents this association by
    listing the association's ID (which can be used for removing the
    association) and linking to the review request.
    """
    name = 'watched_review_request'
    uri_name = 'review-requests'
    profile_field = 'starred_review_requests'
    star_function = 'star_review_request'
    unstar_function = 'unstar_review_request'

    @property
    def watched_resource(self):
        """Return the watched resource.

        This is implemented as a property in order to work around
        a circular reference issue.
        """
        return review_request_resource

    @augment_method_from(BaseWatchedObjectResource)
    def get(self, *args, **kwargs):
        """Returned an :http:`302` pointing to the review request being
        watched.

        Rather than returning a body with the entry, performing an HTTP GET
        on this resource will redirect the client to the actual review request
        being watched.

        Clients must properly handle :http:`302` and expect this redirect
        to happen.
        """
        pass

    @augment_method_from(BaseWatchedObjectResource)
    def get_list(self, *args, **kwargs):
        """Retrieves the list of watched review requests.

        Each entry in the list consists of a numeric ID that represents the
        entry for the watched review request. This is not necessarily the ID
        of the review request itself. It's used for looking up the resource
        of the watched item so that it can be removed.
        """
        pass

    @augment_method_from(BaseWatchedObjectResource)
    def create(self, *args, **kwargs):
        """Marks a review request as being watched.

        The ID of the review group must be passed as ``object_id``, and will
        store that review group in the list.
        """
        pass

    @augment_method_from(BaseWatchedObjectResource)
    def delete(self, *args, **kwargs):
        """Deletes a watched review request entry.

        This is the same effect as unstarring a review request. It does
        not actually delete the review request, just the entry in the list.
        """
        pass

watched_review_request_resource = WatchedReviewRequestResource()


class WatchedResource(WebAPIResource):
    """
    Links to all Watched Items resources for the user.

    This is more of a linking resource rather than a data resource, much like
    the root resource is. The sole purpose of this resource is for easy
    navigation to the more specific Watched Items resources.
    """
    name = 'watched'
    singleton = True

    list_child_resources = [
        watched_review_group_resource,
        watched_review_request_resource,
    ]

    @webapi_check_login_required
    def get_list(self, request, *args, **kwargs):
        """Retrieves the list of Watched Items resources.

        Unlike most resources, the result of this resource is just a list of
        links, rather than any kind of data. It exists in order to index the
        more specific Watched Review Groups and Watched Review Requests
        resources.
        """
        return super(WatchedResource, self).get_list(request, *args, **kwargs)

watched_resource = WatchedResource()


class UserResource(WebAPIResource, DjbletsUserResource):
    """Provides information on registered users."""
    item_child_resources = [
        watched_resource,
    ]

    def get_queryset(self, request, *args, **kwargs):
        search_q = request.GET.get('q', None)

        query = self.model.objects.filter(is_active=True)

        if search_q:
            q = Q(username__istartswith=search_q)

            if request.GET.get('fullname', None):
                q = q | (Q(first_name__istartswith=query) |
                         Q(last_name__istartswith=query))

            query = query.filter(q)

        return query

    @webapi_request_fields(
        optional={
            'q': {
                'type': str,
                'description': 'The string that the username (or the first '
                               'name or last name when using ``fullname``) '
                               'must start with in order to be included in '
                               'the list. This is case-insensitive.',
            },
            'fullname': {
                'type': bool,
                'description': 'Specifies whether ``q`` should also match '
                               'the beginning of the first name or last name.'
            },
        },
        allow_unknown=True
    )
    @augment_method_from(WebAPIResource)
    def get_list(self, *args, **kwargs):
        """Retrieves the list of users on the site.

        This includes only the users who have active accounts on the site.
        Any account that has been disabled (for inactivity, spam reasons,
        or anything else) will be excluded from the list.

        The list of users can be filtered down using the ``q`` and
        ``fullname`` parameters.

        Setting ``q`` to a value will by default limit the results to
        usernames starting with that value. This is a case-insensitive
        comparison.

        If ``fullname`` is set to ``1``, the first and last names will also be
        checked along with the username. ``fullname`` is ignored if ``q``
        is not set.

        For example, accessing ``/api/users/?q=bo&fullname=1`` will list
        any users with a username, first name or last name starting with
        ``bo``.
        """
        pass

    @augment_method_from(WebAPIResource)
    def get(self, *args, **kwargs):
        """Retrieve information on a registered user.

        This mainly returns some basic information (username, full name,
        e-mail address) and links to that user's root Watched Items resource,
        which is used for keeping track of the groups and review requests
        that the user has "starred".
        """
        pass

user_resource = UserResource()


class ReviewGroupUserResource(UserResource):
    """Provides information on users that are members of a review group."""
    uri_object_key = None

    def get_queryset(self, request, group_name, *args, **kwargs):
        return self.model.objects.filter(review_groups__name=group_name)

    @augment_method_from(WebAPIResource)
    def get_list(self, *args, **kwargs):
        """Retrieves the list of users belonging to a specific review group.

        This includes only the users who have active accounts on the site.
        Any account that has been disabled (for inactivity, spam reasons,
        or anything else) will be excluded from the list.

        The list of users can be filtered down using the ``q`` and
        ``fullname`` parameters.

        Setting ``q`` to a value will by default limit the results to
        usernames starting with that value. This is a case-insensitive
        comparison.

        If ``fullname`` is set to ``1``, the first and last names will also be
        checked along with the username. ``fullname`` is ignored if ``q``
        is not set.

        For example, accessing ``/api/users/?q=bo&fullname=1`` will list
        any users with a username, first name or last name starting with
        ``bo``.
        """
        pass

review_group_user_resource = ReviewGroupUserResource()


class ReviewGroupResource(WebAPIResource):
    """Provides information on review groups.

    Review groups are groups of users that can be listed as an intended
    reviewer on a review request.

    Review groups cannot be created, deleted, or modified through the API.
    """
    model = Group
    fields = {
        'id': {
            'type': int,
            'description': 'The numeric ID of the review group.',
        },
        'name': {
            'type': str,
            'description': 'The short name of the group, used in the '
                           'reviewer list and the Dashboard.',
        },
        'display_name': {
            'type': str,
            'description': 'The human-readable name of the group, sometimes '
                           'used as a short description.',
        },
        'invite_only': {
            'type': bool,
            'description': 'Whether or not the group is invite-only. An '
                           'invite-only group is only accessible by members '
                           'of the group.',
        },
        'mailing_list': {
            'type': str,
            'description': 'The e-mail address that all posts on a review '
                           'group are sent to.',
        },
        'url': {
            'type': str,
            'description': "The URL to the user's page on the site. "
                           "This is deprecated and will be removed in a "
                           "future version.",
        },
        'visible': {
            'type': bool,
            'description': 'Whether or not the group is visible to users '
                           'who are not members. This does not prevent users '
                           'from accessing the group if they know it, though.',
        },
    }

    item_child_resources = [
        review_group_user_resource
    ]

    uri_object_key = 'group_name'
    uri_object_key_regex = '[A-Za-z0-9_-]+'
    model_object_key = 'name'

    allowed_methods = ('GET',)

    def get_queryset(self, request, is_list=False, *args, **kwargs):
        search_q = request.GET.get('q', None)

        if is_list:
            query = self.model.objects.accessible(request.user)
        else:
            query = self.model.objects.all()

        if search_q:
            q = Q(name__istartswith=search_q)

            if request.GET.get('displayname', None):
                q = q | Q(display_name__istartswith=search_q)

            query = query.filter(q)

        return query

    def serialize_url_field(self, group):
        return group.get_absolute_url()

    def has_access_permissions(self, request, group, *args, **kwargs):
        return group.is_accessible_by(request.user)

    @augment_method_from(WebAPIResource)
    def get(self, *args, **kwargs):
        """Retrieve information on a review group.

        Some basic information on the review group is provided, including
        the name, description, and mailing list (if any) that e-mails to
        the group are sent to.

        The group links to the list of users that are members of the group.
        """
        pass

    @webapi_request_fields(
        optional={
            'q': {
                'type': str,
                'description': 'The string that the group name (or the  '
                               'display name when using ``displayname``) '
                               'must start with in order to be included in '
                               'the list. This is case-insensitive.',
            },
            'displayname': {
                'type': bool,
                'description': 'Specifies whether ``q`` should also match '
                               'the beginning of the display name.'
            },
        },
        allow_unknown=True
    )
    @augment_method_from(WebAPIResource)
    def get_list(self, *args, **kwargs):
        """Retrieves the list of review groups on the site.

        The list of review groups can be filtered down using the ``q`` and
        ``displayname`` parameters.

        Setting ``q`` to a value will by default limit the results to
        group names starting with that value. This is a case-insensitive
        comparison.

        If ``displayname`` is set to ``1``, the display names will also be
        checked along with the username. ``displayname`` is ignored if ``q``
        is not set.

        For example, accessing ``/api/groups/?q=dev&displayname=1`` will list
        any groups with a name or display name starting with ``dev``.
        """
        pass

review_group_resource = ReviewGroupResource()


class RepositoryInfoResource(WebAPIResource):
    """Provides server-side information on a repository.

    Some repositories can return custom server-side information.
    This is not available for all types of repositories. The information
    will be specific to that type of repository.
    """
    name = 'info'
    singleton = True
    allowed_methods = ('GET',)

    @webapi_check_login_required
    @webapi_response_errors(DOES_NOT_EXIST, REPO_NOT_IMPLEMENTED,
                            REPO_INFO_ERROR)
    def get(self, request, *args, **kwargs):
        """Returns repository-specific information from a server."""
        try:
            repository = repository_resource.get_object(request, *args,
                                                        **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        try:
            tool = repository.get_scmtool()

            return 200, {
                self.item_result_key: tool.get_repository_info()
            }
        except NotImplementedError:
            return REPO_NOT_IMPLEMENTED
        except:
            return REPO_INFO_ERROR

repository_info_resource = RepositoryInfoResource()


class RepositoryResource(WebAPIResource):
    """Provides information on a registered repository.

    Review Board has a list of known repositories, which can be modified
    through the site's administration interface. These repositories contain
    the information needed for Review Board to access the files referenced
    in diffs.
    """
    model = Repository
    name_plural = 'repositories'
    fields = {
        'id': {
            'type': int,
            'description': 'The numeric ID of the repository.',
        },
        'name': {
            'type': str,
            'description': 'The name of the repository.',
        },
        'path': {
            'type': str,
            'description': 'The main path to the repository, which is used '
                           'for communicating with the repository and '
                           'accessing files.',
        },
        'tool': {
            'type': str,
            'description': 'The name of the internal repository '
                           'communication class used to talk to the '
                           'repository. This is generally the type of the '
                           'repository.'
        }
    }
    uri_object_key = 'repository_id'
    item_child_resources = [repository_info_resource]

    allowed_methods = ('GET',)

    @webapi_check_login_required
    def get_queryset(self, request, *args, **kwargs):
        return self.model.objects.accessible(request.user)

    def serialize_tool_field(self, obj):
        return obj.tool.name

    def has_access_permissions(self, request, repository, *args, **kwargs):
        return repository.is_accessible_by(request.user)

    @augment_method_from(WebAPIResource)
    def get_list(self, *args, **kwargs):
        """Retrieves the list of repositories on the server.

        This will only list visible repositories. Any repository that the
        administrator has hidden will be excluded from the list.
        """
        pass

    @augment_method_from(WebAPIResource)
    def get(self, *args, **kwargs):
        """Retrieves information on a particular repository.

        This will only return basic information on the repository.
        Authentication information, hosting details, and repository-specific
        information are not provided.
        """
        pass

repository_resource = RepositoryResource()


class BaseScreenshotResource(WebAPIResource):
    """A base resource representing screenshots."""
    model = Screenshot
    name = 'screenshot'
    fields = {
        'id': {
            'type': int,
            'description': 'The numeric ID of the screenshot.',
        },
        'caption': {
            'type': str,
            'description': "The screenshot's descriptive caption.",
        },
        'path': {
            'type': str,
            'description': "The path of the screenshot's image file, "
                           "relative to the media directory configured "
                           "on the Review Board server.",
        },
        'url': {
            'type': str,
            'description': "The URL of the screenshot file. If this is not "
                           "an absolute URL (for example, if it is just a "
                           "path), then it's relative to the Review Board "
                           "server's URL.",
        },
        'thumbnail_url': {
            'type': str,
            'description': "The URL of the screenshot's thumbnail file. "
                           "If this is not an absolute URL (for example, "
                           "if it is just a path), then it's relative to "
                           "the Review Board server's URL.",
        },
    }

    uri_object_key = 'screenshot_id'

    def get_queryset(self, request, review_request_id, *args, **kwargs):
        return self.model.objects.filter(review_request=review_request_id)

    def serialize_path_field(self, obj):
        return obj.image.name

    def serialize_url_field(self, obj):
        return obj.image.url

    def serialize_thumbnail_url_field(self, obj):
        return obj.get_thumbnail_url()

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED,
                            INVALID_FORM_DATA)
    @webapi_request_fields(
        required={
            'path': {
                'type': file,
                'description': 'The screenshot to upload.',
            },
        },
        optional={
            'caption': {
                'type': str,
                'description': 'The optional caption describing the '
                               'screenshot.',
            },
        },
    )
    def create(self, request, *args, **kwargs):
        """Creates a new screenshot from an uploaded file.

        This accepts any standard image format (PNG, GIF, JPEG) and associates
        it with a draft of a review request.

        It is expected that the client will send the data as part of a
        :mimetype:`multipart/form-data` mimetype. The screenshot's name
        and content should be stored in the ``path`` field. A typical request
        may look like::

            -- SoMe BoUnDaRy
            Content-Disposition: form-data; name=path; filename="foo.png"

            <PNG content here>
            -- SoMe BoUnDaRy --
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_request.is_mutable_by(request.user):
            return PERMISSION_DENIED

        form_data = request.POST.copy()
        form = UploadScreenshotForm(form_data, request.FILES)

        if not form.is_valid():
            return WebAPIResponseFormError(request, form)

        try:
            screenshot = form.create(request.FILES['path'], review_request)
        except ValueError, e:
            return INVALID_FORM_DATA, {
                'fields': {
                    'path': [str(e)],
                },
            }

        return 201, {
            self.item_result_key: screenshot,
        }

    @webapi_login_required
    @webapi_request_fields(
        optional={
            'caption': {
                'type': str,
                'description': 'The new caption for the screenshot.',
            },
        }
    )
    def update(self, request, caption=None, *args, **kwargs):
        """Updates the screenshot's data.

        This allows updating the screenshot in a draft. The caption, currently,
        is the only thing that can be updated.
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
            screenshot = screenshot_resource.get_object(request, *args,
                                                        **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_request.is_mutable_by(request.user):
            return PERMISSION_DENIED

        try:
            review_request_draft_resource.prepare_draft(request,
                                                        review_request)
        except PermissionDenied:
            return PERMISSION_DENIED

        screenshot.draft_caption = caption
        screenshot.save()

        return 200, {
            self.item_result_key: screenshot,
        }


class DraftScreenshotResource(BaseScreenshotResource):
    """Provides information on new screenshots being added to a draft of
    a review request.

    These are screenshots that will be shown once the pending review request
    draft is published.
    """
    name = 'draft_screenshot'
    uri_name = 'screenshots'
    model_parent_key = 'drafts'
    allowed_methods = ('GET', 'DELETE', 'POST', 'PUT',)

    def get_queryset(self, request, review_request_id, *args, **kwargs):
        try:
            draft = review_request_draft_resource.get_object(
                request, review_request_id, *args, **kwargs)

            inactive_ids = \
                draft.inactive_screenshots.values_list('pk', flat=True)

            q = Q(review_request=review_request_id) | Q(drafts=draft)
            query = self.model.objects.filter(q)
            query = query.exclude(pk__in=inactive_ids)
            return query
        except ObjectDoesNotExist:
            return self.model.objects.none()

    def serialize_caption_field(self, obj):
        return obj.draft_caption or obj.caption

    @webapi_login_required
    @augment_method_from(WebAPIResource)
    def get(self, *args, **kwargs):
        pass

    @webapi_login_required
    @augment_method_from(WebAPIResource)
    def delete(self, *args, **kwargs):
        """Deletes the screenshot from the draft.

        This will remove the screenshot from the draft review request.
        This cannot be undone.

        This can be used to remove old screenshots that were previously
        shown, as well as newly added screenshots that were part of the
        draft.

        Instead of a payload response on success, this will return :http:`204`.
        """
        pass

    @webapi_login_required
    @augment_method_from(WebAPIResource)
    def get_list(self, *args, **kwargs):
        """Returns a list of draft screenshots.

        Each screenshot in this list is an uploaded screenshot that will
        be shown in the final review request. These may include newly
        uploaded screenshots or screenshots that were already part of the
        existing review request. In the latter case, existing screenshots
        are shown so that their captions can be added.
        """
        pass

    def _get_list_impl(self, request, *args, **kwargs):
        """Returns the list of screenshots on this draft.

        This is a specialized version of the standard get_list function
        that uses this resource to serialize the children, in order to
        guarantee that we'll be able to identify them as screenshots that are
        part of the draft.
        """
        return WebAPIResponsePaginated(
            request,
            queryset=self.get_queryset(request, is_list=True,
                                       *args, **kwargs),
            results_key=self.list_result_key,
            serialize_object_func=
                lambda obj: self.serialize_object(obj, request=request,
                                                  *args, **kwargs),
            extra_data={
                'links': self.get_links(self.list_child_resources,
                                        request=request, *args, **kwargs),
            })

draft_screenshot_resource = DraftScreenshotResource()


class ReviewRequestDraftResource(WebAPIResource):
    """An editable draft of a review request.

    This resource is used to actually modify a review request. Anything made
    in this draft can be published in order to become part of the public
    review request, or it can be discarded.

    Any POST or PUTs on this draft will cause the draft to be created
    automatically. An initial POST is not required.

    There is only ever a maximum of one draft per review request.

    In order to access this resource, the user must either own the review
    request, or it must have the ``reviews.can_edit_reviewrequest`` permission
    set.
    """
    model = ReviewRequestDraft
    name = 'draft'
    singleton = True
    model_parent_key = 'review_request'
    fields = {
        'id': {
            'type': int,
            'description': 'The numeric ID of the draft.',
            'mutable': False,
        },
        'review_request': {
            'type': 'reviewboard.webapi.resources.ReviewRequestResource',
            'description': 'The review request that owns this draft.',
            'mutable': False,
        },
        'last_updated': {
            'type': str,
            'description': 'The date and time that the draft was last updated '
                           '(in YYYY-MM-DD HH:MM:SS format).',
            'mutable': False,
        },
        'branch': {
            'type': str,
            'description': 'The branch name.',
        },
        'bugs_closed': {
            'type': str,
            'description': 'The new list of bugs closed or referenced by this '
                           'change.',
        },
        'changedescription': {
            'type': str,
            'description': 'A custom description of what changes are being '
                           'made in this update. It often will be used to '
                           'describe the changes in the diff.',
        },
        'description': {
            'type': str,
            'description': 'The new review request description.',
        },
        'public': {
            'type': bool,
            'description': 'Whether or not the draft is public. '
                           'This will always be false up until the time '
                           'it is first made public. At that point, the '
                           'draft is deleted.',
        },
        'summary': {
            'type': str,
            'description': 'The new review request summary.',
        },
        'target_groups': {
            'type': str,
            'description': 'A comma-separated list of review groups '
                           'that will be on the reviewer list.',
        },
        'target_people': {
            'type': str,
            'description': 'A comma-separated list of users that will '
                           'be on a reviewer list.',
        },
        'testing_done': {
            'type': str,
            'description': 'The new testing done text.',
        },
    }

    allowed_methods = ('GET', 'POST', 'PUT', 'DELETE')

    item_child_resources = [
        draft_screenshot_resource,
    ]

    @classmethod
    def prepare_draft(self, request, review_request):
        """Creates a draft, if the user has permission to."""
        if not review_request.is_mutable_by(request.user):
            raise PermissionDenied

        return ReviewRequestDraft.create(review_request)

    def get_queryset(self, request, review_request_id, *args, **kwargs):
        return self.model.objects.filter(review_request=review_request_id)

    def serialize_bugs_closed_field(self, obj):
        return obj.get_bug_list()

    def serialize_changedescription_field(self, obj):
        if obj.changedesc:
            return obj.changedesc.text
        else:
            return ''

    def serialize_status_field(self, obj):
        return status_to_string(obj.status)

    def serialize_public_field(self, obj):
        return False

    def has_delete_permissions(self, request, draft, *args, **kwargs):
        return draft.review_request.is_mutable_by(request.user)

    @webapi_login_required
    @webapi_request_fields(
        optional={
            'branch': {
                'type': str,
                'description': 'The new branch name.',
            },
            'bugs_closed': {
                'type': str,
                'description': 'A comma-separated list of bug IDs.',
            },
            'changedescription': {
                'type': str,
                'description': 'The change description for this update.',
            },
            'description': {
                'type': str,
                'description': 'The new review request description.',
            },
            'public': {
                'type': bool,
                'description': 'Whether or not to make the review public. '
                               'If a review is public, it cannot be made '
                               'private again.',
            },
            'summary': {
                'type': str,
                'description': 'The new review request summary.',
            },
            'target_groups': {
                'type': str,
                'description': 'A comma-separated list of review groups '
                               'that will be on the reviewer list.',
            },
            'target_people': {
                'type': str,
                'description': 'A comma-separated list of users that will '
                               'be on a reviewer list.',
            },
            'testing_done': {
                'type': str,
                'description': 'The new testing done text.',
            },
        },
    )
    def create(self, *args, **kwargs):
        """Creates a draft of a review request.

        If a draft already exists, this will just reuse the existing draft.
        """
        # A draft is a singleton. Creating and updating it are the same
        # operations in practice.
        result = self.update(*args, **kwargs)

        if isinstance(result, tuple):
            if result[0] == 200:
                return (201,) + result[1:]

        return result

    @webapi_login_required
    @webapi_request_fields(
        optional={
            'branch': {
                'type': str,
                'description': 'The new branch name.',
            },
            'bugs_closed': {
                'type': str,
                'description': 'A comma-separated list of bug IDs.',
            },
            'changedescription': {
                'type': str,
                'description': 'The change description for this update.',
            },
            'description': {
                'type': str,
                'description': 'The new review request description.',
            },
            'public': {
                'type': bool,
                'description': 'Whether or not to make the changes public. '
                               'The new changes will be applied to the '
                               'review request, and the old draft will be '
                               'deleted.',
            },
            'summary': {
                'type': str,
                'description': 'The new review request summary.',
            },
            'target_groups': {
                'type': str,
                'description': 'A comma-separated list of review groups '
                               'that will be on the reviewer list.',
            },
            'target_people': {
                'type': str,
                'description': 'A comma-separated list of users that will '
                               'be on a reviewer list.',
            },
            'testing_done': {
                'type': str,
                'description': 'The new testing done text.',
            },
        },
    )
    def update(self, request, always_save=False, *args, **kwargs):
        """Updates a draft of a review request.

        This will update the draft with the newly provided data.

        Most of the fields correspond to fields in the review request, but
        there is one special one, ``public``. When ``public`` is set to ``1``,
        the draft will be published, moving the new content to the
        Review Request itself, making it public, and sending out a notification
        (such as an e-mail) if configured on the server. The current draft will
        then be deleted.
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
        except ReviewRequest.DoesNotExist:
            return DOES_NOT_EXIST

        try:
            draft = self.prepare_draft(request, review_request)
        except PermissionDenied:
            return PERMISSION_DENIED

        modified_objects = []
        invalid_fields = {}

        for field_name, field_info in self.fields.iteritems():
            if (field_info.get('mutable', True) and
                kwargs.get(field_name, None) is not None):
                field_result, field_modified_objects, invalid = \
                    self._set_draft_field_data(draft, field_name,
                                               kwargs[field_name])

                if invalid:
                    invalid_fields[field_name] = invalid
                elif field_modified_objects:
                    modified_objects += field_modified_objects

        if always_save or not invalid_fields:
            for obj in modified_objects:
                obj.save()

            draft.save()

        if invalid_fields:
            return INVALID_FORM_DATA, {
                'fields': invalid_fields,
            }

        if request.POST.get('public', False):
            review_request.publish(user=request.user)

            return 303, {}, {
                'Location': review_request_resource.get_href(
                    review_request, request, *args, **kwargs)
            }
        else:
            return 200, {
                self.item_result_key: draft,
            }

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    def delete(self, request, review_request_id, *args, **kwargs):
        """Deletes a draft of a review request.

        This is equivalent to pressing :guilabel:`Discard Draft` in the
        review request's page. It will simply erase all the contents of
        the draft.
        """
        # Make sure this exists. We don't want to use prepare_draft, or
        # we'll end up creating a new one.
        try:
            draft = ReviewRequestDraft.objects.get(
                review_request=review_request_id)
        except ReviewRequestDraft.DoesNotExist:
            return DOES_NOT_EXIST

        if not self.has_delete_permissions(request, draft, *args, **kwargs):
            return PERMISSION_DENIED

        draft.delete()

        return 204, {}

    @webapi_login_required
    @augment_method_from(WebAPIResource)
    def get(self, request, review_request_id, *args, **kwargs):
        """Returns the current draft of a review request."""
        pass

    def _set_draft_field_data(self, draft, field_name, data):
        """Sets a field on a draft.

        This will update a draft's field based on the provided data.
        It handles transforming the data as necessary to put it into
        the field.

        if there is a problem with the data, then a validation error
        is returned.

        This returns a tuple of (data, modified_objects, invalid_entries).

        ``data`` is the transformed data.

        ``modified_objects`` is a list of objects (screenshots or change
        description) that were affected.

        ``invalid_entries`` is a list of validation errors.
        """
        modified_objects = []
        invalid_entries = []

        if field_name in ('target_groups', 'target_people'):
            values = re.split(r",\s*", data)
            target = getattr(draft, field_name)
            target.clear()

            for value in values:
                # Prevent problems if the user leaves a trailing comma,
                # generating an empty value.
                if not value:
                    continue

                try:
                    if field_name == "target_groups":
                        obj = Group.objects.get((Q(name__iexact=value) |
                                                 Q(display_name__iexact=value)) &
                                                Q(local_site=None))
                    elif field_name == "target_people":
                        obj = self._find_user(username=value)

                    target.add(obj)
                except:
                    invalid_entries.append(value)
        elif field_name == 'bugs_closed':
            data = list(self._sanitize_bug_ids(data))
            setattr(draft, field_name, ','.join(data))
        elif field_name == 'changedescription':
            if not draft.changedesc:
                invalid_entries.append('Change descriptions cannot be used '
                                       'for drafts of new review requests')
            else:
                draft.changedesc.text = data

                modified_objects.append(draft.changedesc)
        else:
            if field_name == 'summary' and '\n' in data:
                invalid_entries.append('Summary cannot contain newlines')
            else:
                setattr(draft, field_name, data)

        return data, modified_objects, invalid_entries

    def _sanitize_bug_ids(self, entries):
        """Sanitizes bug IDs.

        This will remove any excess whitespace before or after the bug
        IDs, and remove any leading ``#`` characters.
        """
        for bug in entries.split(','):
            bug = bug.strip()

            if bug:
                # RB stores bug numbers as numbers, but many people have the
                # habit of prepending #, so filter it out:
                if bug[0] == '#':
                    bug = bug[1:]

                yield bug

    def _find_user(self, username):
        """Finds a User object matching ``username``.

        This will search all authentication backends, and may create the
        User object if the authentication backend knows that the user exists.
        """
        username = username.strip()

        try:
            return User.objects.get(username=username)
        except User.DoesNotExist:
            for backend in auth.get_backends():
                try:
                    user = backend.get_or_create_user(username)
                except:
                    pass

                if user:
                    return user

        return None

review_request_draft_resource = ReviewRequestDraftResource()


class BaseScreenshotCommentResource(WebAPIResource):
    """A base resource for screenshot comments."""
    model = ScreenshotComment
    name = 'screenshot_comment'
    fields = {
        'id': {
            'type': int,
            'description': 'The numeric ID of the comment.',
        },
        'screenshot': {
            'type': 'reviewboard.webapi.resources.ScreenshotResource',
            'description': 'The screenshot the comment was made on.',
        },
        'text': {
            'type': str,
            'description': 'The comment text.',
        },
        'timestamp': {
            'type': str,
            'description': 'The date and time that the comment was made '
                           '(in YYYY-MM-DD HH:MM:SS format).',
        },
        'public': {
            'type': bool,
            'description': 'Whether or not the comment is part of a public '
                           'review.',
        },
        'user': {
            'type': 'reviewboard.webapi.resources.UserResource',
            'description': 'The user who made the comment.',
        },
        'x': {
            'type': int,
            'description': 'The X location of the comment region on the '
                           'screenshot.',
        },
        'y': {
            'type': int,
            'description': 'The Y location of the comment region on the '
                           'screenshot.',
        },
        'w': {
            'type': int,
            'description': 'The width of the comment region on the '
                           'screenshot.',
        },
        'h': {
            'type': int,
            'description': 'The height of the comment region on the '
                           'screenshot.',
        },
    }

    uri_object_key = 'comment_id'

    allowed_methods = ('GET',)

    def get_queryset(self, request, review_request_id, *args, **kwargs):
        return self.model.objects.filter(
            screenshot__review_request=review_request_id,
            review__isnull=False)

    def serialize_public_field(self, obj):
        return obj.review.get().public

    def serialize_timesince_field(self, obj):
        return timesince(obj.timestamp)

    def serialize_user_field(self, obj):
        return obj.review.get().user

    @augment_method_from(WebAPIResource)
    def get(self, *args, **kwargs):
        """Returns information on the comment.

        This contains the comment text, time the comment was made,
        and the location of the comment region on the screenshot, amongst
        other information. It can be used to reconstruct the exact
        position of the comment for use as an overlay on the screenshot.
        """
        pass


class ScreenshotCommentResource(BaseScreenshotCommentResource):
    """Provides information on screenshots comments made on a review request.

    The list of comments cannot be modified from this resource. It's meant
    purely as a way to see existing comments that were made on a diff. These
    comments will span all public reviews.
    """
    model_parent_key = 'screenshot'
    uri_object_key = None

    def get_queryset(self, request, review_request_id, screenshot_id,
                     *args, **kwargs):
        q = super(ScreenshotCommentResource, self).get_queryset(
            request, review_request_id, *args, **kwargs)
        q = q.filter(screenshot=screenshot_id)
        return q

    @augment_method_from(BaseDiffCommentResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of screenshot comments on a screenshot.

        This list of comments will cover all comments made on this
        screenshot from all reviews.
        """
        pass

screenshot_comment_resource = ScreenshotCommentResource()


class ReviewScreenshotCommentResource(BaseScreenshotCommentResource):
    """Provides information on screenshots comments made on a review.

    If the review is a draft, then comments can be added, deleted, or
    changed on this list. However, if the review is already published,
    then no changes can be made.
    """
    allowed_methods = ('GET', 'POST', 'PUT', 'DELETE')
    model_parent_key = 'review'

    def get_queryset(self, request, review_request_id, review_id,
                     *args, **kwargs):
        q = super(ReviewScreenshotCommentResource, self).get_queryset(
            request, review_request_id, *args, **kwargs)
        return q.filter(review=review_id)

    def has_delete_permissions(self, request, comment, *args, **kwargs):
        review = comment.review.get()
        return not review.public and review.user == request.user

    @webapi_login_required
    @webapi_request_fields(
        required = {
            'screenshot_id': {
                'type': int,
                'description': 'The ID of the screenshot being commented on.',
            },
            'x': {
                'type': int,
                'description': 'The X location for the comment.',
            },
            'y': {
                'type': int,
                'description': 'The Y location for the comment.',
            },
            'w': {
                'type': int,
                'description': 'The width of the comment region.',
            },
            'h': {
                'type': int,
                'description': 'The height of the comment region.',
            },
            'text': {
                'type': str,
                'description': 'The comment text.',
            },
        },
    )
    def create(self, request, screenshot_id, x, y, w, h, text,
               *args, **kwargs):
        """Creates a screenshot comment on a review.

        This will create a new comment on a screenshot as part of a review.
        The comment contains text and dimensions for the area being commented
        on.
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
            review = review_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_resource.has_modify_permissions(request, review):
            return PERMISSION_DENIED

        try:
            screenshot = Screenshot.objects.get(pk=screenshot_id,
                                                review_request=review_request)
        except ObjectDoesNotExist:
            return INVALID_FORM_DATA, {
                'fields': {
                    'screenshot_id': ['This is not a valid screenshot ID'],
                }
            }

        new_comment = self.model(screenshot=screenshot, x=x, y=y, w=w, h=h,
                                 text=text)
        new_comment.save()

        review.screenshot_comments.add(new_comment)
        review.save()

        return 201, {
            self.item_result_key: new_comment,
        }

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(
        optional = {
            'x': {
                'type': int,
                'description': 'The X location for the comment.',
            },
            'y': {
                'type': int,
                'description': 'The Y location for the comment.',
            },
            'w': {
                'type': int,
                'description': 'The width of the comment region.',
            },
            'h': {
                'type': int,
                'description': 'The height of the comment region.',
            },
            'text': {
                'type': str,
                'description': 'The comment text.',
            },
        },
    )
    def update(self, request, *args, **kwargs):
        """Updates a screenshot comment.

        This can update the text or region of an existing comment. It
        can only be done for comments that are part of a draft review.
        """
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            review = review_resource.get_object(request, *args, **kwargs)
            screenshot_comment = self.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_resource.has_modify_permissions(request, review):
            return PERMISSION_DENIED

        for field in ('x', 'y', 'w', 'h', 'text'):
            value = kwargs.get(field, None)

            if value is not None:
                setattr(screenshot_comment, field, value)

        screenshot_comment.save()

        return 200, {
            self.item_result_key: screenshot_comment,
        }

    @augment_method_from(BaseScreenshotCommentResource)
    def delete(self, *args, **kwargs):
        """Deletes the comment.

        This will remove the comment from the review. This cannot be undone.

        Only comments on draft reviews can be deleted. Attempting to delete
        a published comment will return a Permission Denied error.

        Instead of a payload response on success, this will return :http:`204`.
        """
        pass

    @augment_method_from(BaseScreenshotCommentResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of screenshot comments made on a review."""
        pass

review_screenshot_comment_resource = ReviewScreenshotCommentResource()


class ReviewReplyScreenshotCommentResource(BaseScreenshotCommentResource):
    """Provides information on replies to screenshot comments made on a
    review reply.

    If the reply is a draft, then comments can be added, deleted, or
    changed on this list. However, if the reply is already published,
    then no changed can be made.
    """
    allowed_methods = ('GET', 'POST', 'PUT', 'DELETE')
    model_parent_key = 'review'
    fields = dict({
        'reply_to': {
            'type': ReviewScreenshotCommentResource,
            'description': 'The comment being replied to.',
        },
    }, **BaseScreenshotCommentResource.fields)

    def get_queryset(self, request, review_request_id, review_id, reply_id,
                     *args, **kwargs):
        q = super(ReviewReplyScreenshotCommentResource, self).get_queryset(
            request, review_request_id, *args, **kwargs)
        q = q.filter(review=reply_id, review__base_reply_to=review_id)
        return q

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, INVALID_FORM_DATA,
                            PERMISSION_DENIED)
    @webapi_request_fields(
        required = {
            'reply_to_id': {
                'type': int,
                'description': 'The ID of the comment being replied to.',
            },
            'text': {
                'type': str,
                'description': 'The comment text.',
            },
        },
    )
    def create(self, request, reply_to_id, text, *args, **kwargs):
        """Creates a reply to a screenshot comment on a review.

        This will create a reply to a screenshot comment on a review.
        The new comment will contain the same dimensions of the comment
        being replied to, but may contain new text.
        """
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            reply = review_reply_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_reply_resource.has_modify_permissions(request, reply):
            return PERMISSION_DENIED

        try:
            comment = review_screenshot_comment_resource.get_object(
                request,
                comment_id=reply_to_id,
                *args, **kwargs)
        except ObjectDoesNotExist:
            return INVALID_FORM_DATA, {
                'fields': {
                    'reply_to_id': ['This is not a valid screenshot '
                                    'comment ID'],
                }
            }

        new_comment = self.model(screenshot=comment.screenshot,
                                 x=comment.x,
                                 y=comment.y,
                                 w=comment.w,
                                 h=comment.h,
                                 text=text)
        new_comment.save()

        reply.screenshot_comments.add(new_comment)
        reply.save()

        return 201, {
            self.item_result_key: new_comment,
        }

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(
        required = {
            'text': {
                'type': str,
                'description': 'The new comment text.',
            },
        },
    )
    def update(self, request, *args, **kwargs):
        """Updates a reply to a screenshot comment.

        This can only update the text in the comment. The comment being
        replied to cannot change.
        """
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            reply = review_reply_resource.get_object(request, *args, **kwargs)
            screenshot_comment = self.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_reply_resource.has_modify_permissions(request, reply):
            return PERMISSION_DENIED

        for field in ('text',):
            value = kwargs.get(field, None)

            if value is not None:
                setattr(screenshot_comment, field, value)

        screenshot_comment.save()

        return 200, {
            self.item_result_key: screenshot_comment,
        }

    @augment_method_from(BaseScreenshotCommentResource)
    def delete(self, *args, **kwargs):
        """Deletes a screnshot comment from a draft reply.

        This will remove the comment from the reply. This cannot be undone.

        Only comments on draft replies can be deleted. Attempting to delete
        a published comment will return a Permission Denied error.

        Instead of a payload response, this will return :http:`204`.
        """
        pass

    @augment_method_from(BaseScreenshotCommentResource)
    def get(self, *args, **kwargs):
        """Returns information on a reply to a screenshot comment.

        Much of the information will be identical to that of the comment
        being replied to. For example, the region on the screenshot.
        This is because the reply to the comment is meant to cover the
        exact same section of the screenshot that the original comment covers.
        """
        pass

    @augment_method_from(BaseScreenshotCommentResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of replies to screenshot comments made on a
        review reply.
        """
        pass

review_reply_screenshot_comment_resource = \
    ReviewReplyScreenshotCommentResource()


class BaseReviewResource(WebAPIResource):
    """Base class for review resources.

    Provides common fields and functionality for all review resources.
    """
    model = Review
    fields = {
        'body_bottom': {
            'type': str,
            'description': 'The review content below the comments.',
        },
        'body_top': {
            'type': str,
            'description': 'The review content above the comments.',
        },
        'id': {
            'type': int,
            'description': 'The numeric ID of the review.',
        },
        'public': {
            'type': bool,
            'description': 'Whether or not the review is currently '
                           'visible to other users.',
        },
        'ship_it': {
            'type': bool,
            'description': 'Whether or not the review has been marked '
                           '"Ship It!"',
        },
        'timestamp': {
            'type': str,
            'description': 'The date and time that the review was posted '
                           '(in YYYY-MM-DD HH:MM:SS format).',
        },
        'user': {
            'type': UserResource,
            'description': 'The user who wrote the review.',
        },
    }

    allowed_methods = ('GET', 'POST', 'PUT', 'DELETE')

    def get_queryset(self, request, review_request_id, is_list=False,
                     *args, **kwargs):
        q = Q(review_request=review_request_id) & \
            Q(**self.get_base_reply_to_field(*args, **kwargs))

        if is_list:
            # We don't want to show drafts in the list.
            q = q & Q(public=True)

        return self.model.objects.filter(q)

    def get_base_reply_to_field(self):
        raise NotImplemented

    def has_access_permissions(self, request, review, *args, **kwargs):
        return review.public or review.user == request.user

    def has_modify_permissions(self, request, review, *args, **kwargs):
        return not review.public and review.user == request.user

    def has_delete_permissions(self, request, review, *args, **kwargs):
        return not review.public and review.user == request.user

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(
        optional = {
            'ship_it': {
                'type': bool,
                'description': 'Whether or not to mark the review "Ship It!"',
            },
            'body_top': {
                'type': str,
                'description': 'The review content above the comments.',
            },
            'body_bottom': {
                'type': str,
                'description': 'The review content below the comments.',
            },
            'public': {
                'type': bool,
                'description': 'Whether or not to make the review public. '
                               'If a review is public, it cannot be made '
                               'private again.',
            },
        },
    )
    def create(self, request, *args, **kwargs):
        """Creates a new review.

        The new review will start off as private. Only the author of the
        review (the user who is logged in and issuing this API call) will
        be able to see and interact with the review.

        Initial data for the review can be provided by passing data for
        any number of the fields. If nothing is provided, the review will
        start off as blank.

        If the user submitting this review already has a pending draft review
        on this review request, then this will update the existing draft and
        return :http:`303`. Otherwise, this will create a new draft and
        return :http:`201`. Either way, this request will return without
        a payload and with a ``Location`` header pointing to the location of
        the new draft review.
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        review, is_new = Review.objects.get_or_create(
            review_request=review_request,
            user=request.user,
            public=False,
            **self.get_base_reply_to_field(*args, **kwargs))

        if is_new:
            status_code = 201 # Created
        else:
            # This already exists. Go ahead and update, but we're going to
            # redirect the user to the right place.
            status_code = 303 # See Other

        result = self._update_review(request, review, *args, **kwargs)

        if not isinstance(result, tuple) or result[0] != 200:
            return result
        else:
            return status_code, result[1], {
                'Location': self.get_href(review, request, *args, **kwargs),
            }

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(
        optional = {
            'ship_it': {
                'type': bool,
                'description': 'Whether or not to mark the review "Ship It!"',
            },
            'body_top': {
                'type': str,
                'description': 'The review content above the comments.',
            },
            'body_bottom': {
                'type': str,
                'description': 'The review content below the comments.',
            },
            'public': {
                'type': bool,
                'description': 'Whether or not to make the review public. '
                               'If a review is public, it cannot be made '
                               'private again.',
            },
        },
    )
    def update(self, request, *args, **kwargs):
        """Updates a review.

        This updates the fields of a draft review. Published reviews cannot
        be updated.

        Only the owner of a review can make changes. One or more fields can
        be updated at once.

        The only special field is ``public``, which, if set to ``1``, will
        publish the review. The review will then be made publicly visible. Once
        public, the review cannot be modified or made private again.
        """
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            review = review_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        return self._update_review(request, review, *args, **kwargs)

    @augment_method_from(WebAPIResource)
    def delete(self, *args, **kwargs):
        """Deletes the draft review.

        This only works for draft reviews, not public reviews. It will
        delete the review and all comments on it. This cannot be undone.

        Only the user who owns the draft can delete it.

        Upon deletion, this will return :http:`204`.
        """
        pass

    @augment_method_from(WebAPIResource)
    def get(self, *args, **kwargs):
        """Returns information on a particular review.

        If the review is not public, then the client's logged in user
        must either be the owner of the review. Otherwise, an error will
        be returned.
        """
        pass

    def _update_review(self, request, review, public=None, *args, **kwargs):
        """Common function to update fields on a draft review."""
        if not self.has_modify_permissions(request, review):
            # Can't modify published reviews or those not belonging
            # to the user.
            return PERMISSION_DENIED

        for field in ('ship_it', 'body_top', 'body_bottom'):
            value = kwargs.get(field, None)

            if value is not None:
                setattr(review, field, value)

        review.save()

        if public:
            review.publish(user=request.user)

        return 200, {
            self.item_result_key: review,
        }


class ReviewReplyDraftResource(WebAPIResource):
    """A redirecting resource that points to the current draft reply.

    This works as a convenience to access the current draft reply, so that
    clients can discover the proper location.
    """
    name = 'reply_draft'
    singleton = True
    uri_name = 'draft'

    @webapi_login_required
    def get(self, request, *args, **kwargs):
        """Returns the location of the current draft reply.

        If the draft reply exists, this will return :http:`301` with
        a ``Location`` header pointing to the URL of the draft. Any
        operations on the draft can be done at that URL.

        If the draft reply does not exist, this will return a Does Not
        Exist error.
        """
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            review = review_resource.get_object(request, *args, **kwargs)
            reply = review.get_pending_reply(request.user)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not reply:
            return DOES_NOT_EXIST

        return 301, {}, {
            'Location': review_reply_resource.get_href(reply, request,
                                                       *args, **kwargs),
        }

review_reply_draft_resource = ReviewReplyDraftResource()


class ReviewReplyResource(BaseReviewResource):
    """Provides information on a reply to a review.

    A reply is much like a review, but is always tied to exactly one
    parent review. Every comment associated with a reply is also tied to
    a parent comment.
    """
    name = 'reply'
    name_plural = 'replies'
    fields = {
        'body_bottom': {
            'type': str,
            'description': 'The response to the review content below '
                           'the comments.',
        },
        'body_top': {
            'type': str,
            'description': 'The response to the review content above '
                           'the comments.',
        },
        'id': {
            'type': int,
            'description': 'The numeric ID of the reply.',
        },
        'public': {
            'type': bool,
            'description': 'Whether or not the reply is currently '
                           'visible to other users.',
        },
        'timestamp': {
            'type': str,
            'description': 'The date and time that the reply was posted '
                           '(in YYYY-MM-DD HH:MM:SS format).',
        },
        'user': {
            'type': UserResource,
            'description': 'The user who wrote the reply.',
        },
    }

    item_child_resources = [
        review_reply_diff_comment_resource,
        review_reply_screenshot_comment_resource,
    ]

    list_child_resources = [
        review_reply_draft_resource,
    ]

    uri_object_key = 'reply_id'
    model_parent_key = 'base_reply_to'

    def get_base_reply_to_field(self, review_id, *args, **kwargs):
        return {
            'base_reply_to': Review.objects.get(pk=review_id),
        }

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(
        optional = {
            'body_top': {
                'type': str,
                'description': 'The response to the review content above '
                               'the comments.',
            },
            'body_bottom': {
                'type': str,
                'description': 'The response to the review content below '
                               'the comments.',
            },
            'public': {
                'type': bool,
                'description': 'Whether or not to make the reply public. '
                               'If a reply is public, it cannot be made '
                               'private again.',
            },
        },
    )
    def create(self, request, *args, **kwargs):
        """Creates a reply to a review.

        The new reply will start off as private. Only the author of the
        reply (the user who is logged in and issuing this API call) will
        be able to see and interact with the reply.

        Initial data for the reply can be provided by passing data for
        any number of the fields. If nothing is provided, the reply will
        start off as blank.

        If the user submitting this reply already has a pending draft reply
        on this review, then this will update the existing draft and
        return :http:`303`. Otherwise, this will create a new draft and
        return :http:`201`. Either way, this request will return without
        a payload and with a ``Location`` header pointing to the location of
        the new draft reply.
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
            review = review_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        reply, is_new = Review.objects.get_or_create(
            review_request=review_request,
            user=request.user,
            public=False,
            base_reply_to=review)

        if is_new:
            status_code = 201 # Created
        else:
            # This already exists. Go ahead and update, but we're going to
            # redirect the user to the right place.
            status_code = 303 # See Other

        result = self._update_reply(request, reply, *args, **kwargs)

        if not isinstance(result, tuple) or result[0] != 200:
            return result
        else:
            return status_code, result[1], {
                'Location': self.get_href(reply, request, *args, **kwargs),
            }

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(
        optional = {
            'body_top': {
                'type': str,
                'description': 'The response to the review content above '
                               'the comments.',
            },
            'body_bottom': {
                'type': str,
                'description': 'The response to the review content below '
                               'the comments.',
            },
            'public': {
                'type': bool,
                'description': 'Whether or not to make the reply public. '
                               'If a reply is public, it cannot be made '
                               'private again.',
            },
        },
    )
    def update(self, request, *args, **kwargs):
        """Updates a reply.

        This updates the fields of a draft reply. Published replies cannot
        be updated.

        Only the owner of a reply can make changes. One or more fields can
        be updated at once.

        The only special field is ``public``, which, if set to ``1``, will
        publish the reply. The reply will then be made publicly visible. Once
        public, the reply cannot be modified or made private again.
        """
        try:
            review_request_resource.get_object(request, *args, **kwargs)
            review_resource.get_object(request, *args, **kwargs)
            reply = self.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        return self._update_reply(request, reply, *args, **kwargs)

    @augment_method_from(BaseReviewResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of all public replies on a review."""
        pass

    @augment_method_from(BaseReviewResource)
    def get(self, *args, **kwargs):
        """Returns information on a particular reply.

        If the reply is not public, then the client's logged in user
        must either be the owner of the reply. Otherwise, an error will
        be returned.
        """
        pass

    def _update_reply(self, request, reply, public=None, *args, **kwargs):
        """Common function to update fields on a draft reply."""
        if not self.has_modify_permissions(request, reply):
            # Can't modify published replies or those not belonging
            # to the user.
            return PERMISSION_DENIED

        for field in ('body_top', 'body_bottom'):
            value = kwargs.get(field, None)

            if value is not None:
                setattr(reply, field, value)

                if value == '':
                    reply_to = None
                else:
                    reply_to = reply.base_reply_to

                setattr(reply, '%s_reply_to' % field, reply_to)

        if public:
            reply.publish(user=request.user)
        else:
            reply.save()

        return 200, {
            self.item_result_key: reply,
        }

review_reply_resource = ReviewReplyResource()


class ReviewDraftResource(WebAPIResource):
    """A redirecting resource that points to the current draft review."""
    name = 'review_draft'
    singleton = True
    uri_name = 'draft'

    @webapi_login_required
    def get(self, request, *args, **kwargs):
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
            review = review_request.get_pending_review(request.user)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review:
            return DOES_NOT_EXIST

        return 301, {}, {
            'Location': review_resource.get_href(review, request,
                                                 *args, **kwargs),
        }

review_draft_resource = ReviewDraftResource()


class ReviewResource(BaseReviewResource):
    """Provides information on reviews."""
    uri_object_key = 'review_id'
    model_parent_key = 'review_request'

    item_child_resources = [
        review_diff_comment_resource,
        review_reply_resource,
        review_screenshot_comment_resource,
    ]

    list_child_resources = [
        review_draft_resource,
    ]

    @augment_method_from(BaseReviewResource)
    def get_list(self, *args, **kwargs):
        """Returns the list of all public reviews on a review request."""
        pass

    def get_base_reply_to_field(self, *args, **kwargs):
        return {
            'base_reply_to__isnull': True,
        }

review_resource = ReviewResource()


class ScreenshotResource(BaseScreenshotResource):
    """A resource representing a screenshot on a review request."""
    model_parent_key = 'review_request'

    item_child_resources = [
        screenshot_comment_resource,
    ]

    allowed_methods = ('GET', 'POST', 'PUT', 'DELETE')

    @augment_method_from(BaseScreenshotResource)
    def get_list(self, *args, **kwargs):
        """Returns a list of screenshots on the review request.

        Each screenshot in this list is an uploaded screenshot that is
        shown on the review request.
        """
        pass

    @augment_method_from(BaseScreenshotResource)
    def create(self, request, *args, **kwargs):
        """Creates a new screenshot from an uploaded file.

        This accepts any standard image format (PNG, GIF, JPEG) and associates
        it with a draft of a review request.

        Creating a new screenshot will automatically create a new review
        request draft, if one doesn't already exist. This screenshot will
        be part of that draft, and will be shown on the review request
        when it's next published.

        It is expected that the client will send the data as part of a
        :mimetype:`multipart/form-data` mimetype. The screenshot's name
        and content should be stored in the ``path`` field. A typical request
        may look like::

            -- SoMe BoUnDaRy
            Content-Disposition: form-data; name=path; filename="foo.png"

            <PNG content here>
            -- SoMe BoUnDaRy --
        """
        pass

    @augment_method_from(BaseScreenshotResource)
    def update(self, request, caption=None, *args, **kwargs):
        """Updates the screenshot's data.

        This allows updating the screenshot. The caption, currently,
        is the only thing that can be updated.

        Updating a screenshot will automatically create a new review request
        draft, if one doesn't already exist. The updates won't be public
        until the review request draft is published.
        """
        pass

    @webapi_login_required
    @augment_method_from(WebAPIResource)
    def delete(self, *args, **kwargs):
        """Deletes the screenshot.

        This will remove the screenshot from the draft review request.
        This cannot be undone.

        Deleting a screenshot will automatically create a new review request
        draft, if one doesn't already exist. The screenshot won't be actually
        removed until the review request draft is published.

        This can be used to remove old screenshots that were previously
        shown, as well as newly added screenshots that were part of the
        draft.

        Instead of a payload response on success, this will return :http:`204`.
        """
        pass

screenshot_resource = ScreenshotResource()


class ReviewRequestLastUpdateResource(WebAPIResource):
    """Provides information on the last update made to a review request.

    Clients can periodically poll this to see if any new updates have been
    made.
    """
    name = 'last_update'
    singleton = True
    allowed_methods = ('GET',)

    fields = {
        'summary': {
            'type': str,
            'description': 'A short summary of the update. This should be one '
                           'of "Review request updated", "Diff updated", '
                           '"New reply" or "New review".',
        },
        'timestamp': {
            'type': str,
            'description': 'The timestamp of this most recent update '
                           '(YYYY-MM-DD HH:MM:SS format).',
        },
        'type': {
            'type': ('review-request', 'diff', 'reply', 'review'),
            'description': "The type of the last update. ``review-request`` "
                           "means the last update was an update of the "
                           "review request's information. ``diff`` means a "
                           "new diff was uploaded. ``reply`` means a reply "
                           "was made to an existing review. ``review`` means "
                           "a new review was posted.",
        },
        'user': {
            'type': str,
            'description': 'The user who made the last update.',
        },
    }

    @webapi_check_login_required
    def get(self, request, *args, **kwargs):
        """Returns the last update made to the review request.

        This shows the type of update that was made, the user who made the
        update, and when the update was made. Clients can use this to inform
        the user that the review request was updated, or automatically update
        it in the background.

        This does not take into account changes to a draft review request, as
        that's generally not update information that the owner of the draft is
        interested in. Only public updates are represented.
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if not review_request_resource.has_access_permissions(request,
                                                              review_request):
            return PERMISSION_DENIED

        timestamp, updated_object = review_request.get_last_activity()
        user = None
        summary = None
        update_type = None

        if isinstance(updated_object, ReviewRequest):
            user = updated_object.submitter
            summary = _("Review request updated")
            update_type = "review-request"
        elif isinstance(updated_object, DiffSet):
            summary = _("Diff updated")
            update_type = "diff"
        elif isinstance(updated_object, Review):
            user = updated_object.user

            if updated_object.is_reply():
                summary = _("New reply")
                update_type = "reply"
            else:
                summary = _("New review")
                update_type = "review"
        else:
            # Should never be able to happen. The object will always at least
            # be a ReviewRequest.
            assert False

        return 200, {
            self.item_result_key: {
                'timestamp': timestamp,
                'user': user,
                'summary': summary,
                'type': update_type,
            }
        }

review_request_last_update_resource = ReviewRequestLastUpdateResource()


class ReviewRequestResource(WebAPIResource):
    """Provides information on review requests."""
    model = ReviewRequest
    name = 'review_request'

    fields = {
        'id': {
            'type': int,
            'description': 'The numeric ID of the review request.',
        },
        'submitter': {
            'type': UserResource,
            'description': 'The user who submitted the review request.',
        },
        'time_added': {
            'type': str,
            'description': 'The date and time that the review request was '
                           'added (in YYYY-MM-DD HH:MM:SS format).',
        },
        'last_updated': {
            'type': str,
            'description': 'The date and time that the review request was '
                           'last updated (in YYYY-MM-DD HH:MM:SS format).',
        },
        'status': {
            'type': ('discarded', 'pending', 'submitted'),
            'description': 'The current status of the review request.',
        },
        'public': {
            'type': bool,
            'description': 'Whether or not the review request is currently '
                           'visible to other users.',
        },
        'changenum': {
            'type': int,
            'description': 'The change number that the review request is '
                           'representing. These are server-side '
                           'repository-specific change numbers, and are not '
                           'supported by all types of repositories. This may '
                           'be ``null``.',
        },
        'repository': {
            'type': RepositoryResource,
            'description': "The repository that the review request's code "
                           "is stored on.",
        },
        'summary': {
            'type': str,
            'description': "The review request's brief summary.",
        },
        'description': {
            'type': str,
            'description': "The review request's description.",
        },
        'testing_done': {
            'type': str,
            'description': 'The information on the testing that was done '
                           'for the change.',
        },
        'bugs_closed': {
            'type': [str],
            'description': 'The list of bugs closed or referenced by this '
                           'change.',
        },
        'branch': {
            'type': str,
            'description': 'The branch that the code was changed on or that '
                           'the code will be committed to. This is a '
                           'free-form field that can store any text.',
        },
        'target_groups': {
            'type': [ReviewGroupResource],
            'description': 'The list of review groups who were requested '
                           'to review this change.',
        },
        'target_people': {
            'type': [UserResource],
            'description': 'The list of users who were requested to review '
                           'this change.',
        },
    }
    uri_object_key = 'review_request_id'
    item_child_resources = [
        diffset_resource,
        review_request_draft_resource,
        review_request_last_update_resource,
        review_resource,
        screenshot_resource,
    ]

    allowed_methods = ('GET', 'POST', 'PUT', 'DELETE')

    _close_type_map = {
        'submitted': ReviewRequest.SUBMITTED,
        'discarded': ReviewRequest.DISCARDED,
    }

    def get_queryset(self, request, is_list=False, *args, **kwargs):
        """Returns a queryset for ReviewRequest models.

        By default, this returns all published or formerly published
        review requests.

        If the queryset is being used for a list of review request
        resources, then it can be further filtered by one or more of the
        following arguments in the URL:

          * ``changenum``
              - The change number the review requests must be
                against. This will only return one review request
                per repository, and only works for repository
                types that support server-side changesets.

          * ``time-added-to``
              - The date/time that all review requests must be added before.
                This is compared against the review request's ``time_added``
                field. See below for information on date/time formats.

          * ``time-added-from``
              - The earliest date/time the review request could be added.
                This is compared against the review request's ``time_added``
                field. See below for information on date/time formats.

          * ``last-updated-to``
              - The date/time that all review requests must be last updated
                before. This is compared against the review request's
                ``last_updated`` field. See below for information on date/time
                formats.

          * ``last-updated-from``
              - The earliest date/time the review request could be last
                updated. This is compared against the review request's
                ``last_updated`` field. See below for information on date/time
                formats.

          * ``from-user``
              - The username that the review requests must be owned by.

          * ``repository``
              - The ID of the repository that the review requests must be on.

          * ``status``
              - The status of the review requests. This can be ``pending``,
                ``submitted`` or ``discarded``.

          * ``to-groups``
              - A comma-separated list of review group names that the review
                requests must have in the reviewer list.

          * ``to-user-groups``
              - A comma-separated list of usernames who are in groups that the
                review requests must have in the reviewer list.

          * ``to-users``
              - A comma-separated list of usernames that the review requests
                must either have in the reviewer list specifically or by way
                of a group.

          * ``to-users-directly``
              - A comma-separated list of usernames that the review requests
                must have in the reviewer list specifically.

        Some arguments accept dates. The handling of dates is quite flexible,
        accepting a variety of date/time formats, but we recommend sticking
        with ISO8601 format.

        ISO8601 format defines a date as being in ``{yyyy}-{mm}-{dd}`` format,
        and a date/time as being in ``{yyyy}-{mm}-{dd}T{HH}:{MM}:{SS}``.
        A timezone can also be appended to this, using ``-{HH:MM}``.

        The following examples are valid dates and date/times:

            * ``2010-06-27``
            * ``2010-06-27T16:26:30``
            * ``2010-06-27T16:26:30-08:00``
        """
        q = Q()

        if is_list:
            if 'to-groups' in request.GET:
                for group_name in request.GET.get('to-groups').split(','):
                    q = q & self.model.objects.get_to_group_query(group_name,
                                                                  None)

            if 'to-users' in request.GET:
                for username in request.GET.get('to-users').split(','):
                    q = q & self.model.objects.get_to_user_query(username)

            if 'to-users-directly' in request.GET:
                for username in request.GET.get('to-users-directly').split(','):
                    q = q & self.model.objects.get_to_user_directly_query(
                        username)

            if 'to-users-groups' in request.GET:
                for username in request.GET.get('to-users-groups').split(','):
                    q = q & self.model.objects.get_to_user_groups_query(
                        username)

            if 'from-user' in request.GET:
                q = q & self.model.objects.get_from_user_query(
                    request.GET.get('from-user'))

            if 'repository' in request.GET:
                q = q & Q(repository=int(request.GET.get('repository')))

            if 'changenum' in request.GET:
                q = q & Q(changenum=int(request.GET.get('changenum')))

            if 'time-added-from' in request.GET:
                date = self._parse_date(request.GET['time-added-from'])

                if date:
                    q = q & Q(time_added__gte=date)

            if 'time-added-to' in request.GET:
                date = self._parse_date(request.GET['time-added-to'])

                if date:
                    q = q & Q(time_added__lt=date)

            if 'last-updated-from' in request.GET:
                date = self._parse_date(request.GET['last-updated-from'])

                if date:
                    q = q & Q(last_updated__gte=date)

            if 'last-updated-to' in request.GET:
                date = self._parse_date(request.GET['last-updated-to'])

                if date:
                    q = q & Q(last_updated__lt=date)

            status = string_to_status(request.GET.get('status', 'pending'))

            return self.model.objects.public(user=request.user, status=status,
                                             extra_query=q)
        else:
            return self.model.objects.all()

    def has_access_permissions(self, request, review_request, *args, **kwargs):
        return review_request.is_accessible_by(request.user)

    def has_delete_permissions(self, request, review_request, *args, **kwargs):
        return request.user.has_perm('reviews.delete_reviewrequest')

    def serialize_bugs_closed_field(self, obj):
        return obj.get_bug_list()

    def serialize_status_field(self, obj):
        return status_to_string(obj.status)

    @webapi_login_required
    @webapi_response_errors(PERMISSION_DENIED, INVALID_USER,
                            INVALID_REPOSITORY, CHANGE_NUMBER_IN_USE,
                            INVALID_CHANGE_NUMBER, EMPTY_CHANGESET)
    @webapi_request_fields(
        required={
            'repository': {
                'type': str,
                'description': 'The path or ID of the repository that the '
                               'review request is for.',
            },
        },
        optional={
            'changenum': {
                'type': int,
                'description': 'The optional changenumber to look up for the '
                               'review request details. This only works with '
                               'repositories that support server-side '
                               'changesets.',
            },
            'submit_as': {
                'type': str,
                'description': 'The optional user to submit the review '
                               'request as. This requires that the actual '
                               'logged in user is either a superuser or has '
                               'the "reviews.can_submit_as_another_user" '
                               'permission.',
            },
        })
    def create(self, request, repository, submit_as=None, changenum=None,
               *args, **kwargs):
        """Creates a new review request.

        The new review request will start off as private and pending, and
        will normally be blank. However, if ``changenum`` is passed and the
        given repository both supports server-side changesets and has changeset
        support in Review Board, some details (Summary, Description and Testing
        Done sections, for instance) may be automatically filled in from the
        server.

        Any new review request will have an associated draft (reachable
        through the ``draft`` link). All the details of the review request
        must be set through the draft. The new review request will be public
        when that first draft is published.

        The only requirement when creating a review request is that a valid
        repository is passed. This can either be a numeric repository ID, or
        the path to a repository (matching exactly the registered repository's
        Path field in the adminstration interface). Failing to pass a valid
        repository will result in an error.

        Clients can create review requests on behalf of another user by setting
        the ``submit_as`` parameter to the username of the desired user. This
        requires that the client is currently logged in as a user that has the
        ``reviews.can_submit_as_another_user`` permission set. This capability
        is useful when writing automation scripts, such as post-commit hooks,
        that need to create review requests for another user.
        """
        user = request.user

        if submit_as and user.username != submit_as:
            if not user.has_perm('reviews.can_submit_as_another_user'):
                return PERMISSION_DENIED

            try:
                user = User.objects.get(username=submit_as)
            except User.DoesNotExist:
                return INVALID_USER

        try:
            try:
                repository = Repository.objects.get(pk=int(repository))
            except ValueError:
                # The repository is not an ID.
                repository = Repository.objects.get(
                    Q(path=repository) |
                    Q(mirror_path=repository))
        except Repository.DoesNotExist, e:
            return INVALID_REPOSITORY, {
                'repository': repository
            }

        if not repository.is_accessible_by(request.user):
            return PERMISSION_DENIED

        try:
            review_request = ReviewRequest.objects.create(user, repository,
                                                          changenum)

            return 201, {
                self.item_result_key: review_request
            }
        except ChangeNumberInUseError, e:
            return CHANGE_NUMBER_IN_USE, {
                'review_request': e.review_request
            }
        except InvalidChangeNumberError:
            return INVALID_CHANGE_NUMBER
        except EmptyChangeSetError:
            return EMPTY_CHANGESET

    @webapi_login_required
    @webapi_response_errors(DOES_NOT_EXIST, PERMISSION_DENIED)
    @webapi_request_fields(
        optional={
            'status': {
                'type': ('discarded', 'pending', 'submitted'),
                'description': 'The status of the review request. This can '
                               'be changed to close or reopen the review '
                               'request',
            },
        },
    )
    def update(self, request, status=None, *args, **kwargs):
        """Updates the status of the review request.

        The only supported update to a review request's resource is to change
        the status, in order to close it as discarded or submitted, or to
        reopen as pending.

        Changes to a review request's fields, such as the summary or the
        list of reviewers, is made on the Review Request Draft resource.
        This can be accessed through the ``draft`` link. Only when that
        draft is published will the changes end up back in this resource.
        """
        try:
            review_request = \
                review_request_resource.get_object(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return DOES_NOT_EXIST

        if (status is not None and
            review_request.status != string_to_status(status)):
            try:
                if status in self._close_type_map:
                    review_request.close(self._close_type_map[status],
                                         request.user)
                elif status == 'pending':
                    review_request.reopen(request.user)
                else:
                    raise AssertionError("Code path for invalid status '%s' "
                                         "should never be reached." % status)
            except PermissionError:
                return PERMISSION_DENIED

        return 200, {
            self.item_result_key: review_request,
        }

    @augment_method_from(WebAPIResource)
    def delete(self, *args, **kwargs):
        """Deletes the review request permanently.

        This is a dangerous call to make, as it will delete the review
        request, associated screenshots, diffs, and reviews. There is no
        going back after this call is made.

        Only users who have been granted the ``reviews.delete_reviewrequest``
        permission (which includes administrators) can perform a delete on
        the review request.

        After a successful delete, this will return :http:`204`.
        """
        pass

    @webapi_request_fields(
        optional={
            'changenum': {
                'type': str,
                'description': 'The change number the review requests must '
                               'have set. This will only return one review '
                               'request per repository, and only works for '
                               'repository types that support server-side '
                               'changesets.',
            },
            'time-added-to': {
                'type': str,
                'description': 'The date/time that all review requests must '
                               'be added before. This is compared against the '
                               'review request\'s ``time_added`` field. This '
                               'must be a valid :term:`date/time format`.',
            },
            'time-added-from': {
                'type': str,
                'description': 'The earliest date/time the review request '
                               'could be added. This is compared against the '
                               'review request\'s ``time_added`` field. This '
                               'must be a valid :term:`date/time format`.',
            },
            'last-updated-to': {
                'type': str,
                'description': 'The date/time that all review requests must '
                               'be last updated before. This is compared '
                               'against the review request\'s '
                               '``last_updated`` field. This must be a valid '
                               ':term:`date/time format`.',
            },
            'last-updated-from': {
                'type': str,
                'description': 'The earliest date/time the review request '
                               'could be last updated. This is compared '
                               'against the review request\'s ``last_updated`` '
                               'field. This must be a valid '
                               ':term:`date/time format`.',
            },
            'from-user': {
                'type': str,
                'description': 'The username that the review requests must '
                               'be owned by.',
            },
            'repository': {
                'type': int,
                'description': 'The ID of the repository that the review '
                                'requests must be on.',
            },
            'status': {
                'type': ('all', 'discarded', 'pending', 'submitted'),
                'description': 'The status of the review requests.'
            },
            'to-groups': {
                'type': str,
                'description': 'A comma-separated list of review group names '
                               'that the review requests must have in the '
                               'reviewer list.',
            },
            'to-user-groups': {
                'type': str,
                'description': 'A comma-separated list of usernames who are '
                               'in groups that the review requests must have '
                               'in the reviewer list.',
            },
            'to-users': {
                'type': str,
                'description': 'A comma-separated list of usernames that the '
                               'review requests must either have in the '
                               'reviewer list specifically or by way of '
                               'a group.',
            },
            'to-users-directly': {
                'type': str,
                'description': 'A comma-separated list of usernames that the '
                               'review requests must have in the reviewer '
                               'list specifically.',
            }
        },
        allow_unknown=True
    )
    @augment_method_from(WebAPIResource)
    def get_list(self, *args, **kwargs):
        """Returns all review requests that the user has read access to.

        By default, this returns all published or formerly published
        review requests.

        The resulting list can be filtered down through the many
        request parameters.
        """
        pass

    @augment_method_from(WebAPIResource)
    def get(self, *args, **kwargs):
        """Returns information on a particular review request.

        This contains full information on the latest published review request.

        If the review request is not public, then the client's logged in user
        must either be the owner of the review request or must have the
        ``reviews.can_edit_reviewrequest`` permission set. Otherwise, an
        error will be returned.
        """
        pass

    def _parse_date(self, timestamp_str):
        try:
            return dateutil.parser.parse(timestamp_str)
        except ValueError:
            return None


review_request_resource = ReviewRequestResource()


class ServerInfoResource(WebAPIResource):
    """Information on the Review Board server.

    This contains product information, such as the version, and
    site-specific information, such as the main URL and list of
    administrators.
    """
    name = 'info'
    singleton = True

    @webapi_check_login_required
    def get(self, request, *args, **kwargs):
        """Returns the information on the Review Board server."""
        site = Site.objects.get_current()
        siteconfig = SiteConfiguration.objects.get_current()

        url = '%s://%s%s' % (siteconfig.get('site_domain_method'), site.domain,
                             settings.SITE_ROOT)

        return 200, {
            self.item_result_key: {
                'product': {
                    'name': 'Review Board',
                    'version': get_version_string(),
                    'package_version': get_package_version(),
                    'is_release': is_release(),
                },
                'site': {
                    'url': url,
                    'administrators': [{'name': name, 'email': email}
                                       for name, email in settings.ADMINS],
                },
            },
        }

server_info_resource = ServerInfoResource()


class SessionResource(WebAPIResource):
    """Information on the active user's session.

    This includes information on the user currently logged in through the
    calling client, if any. Currently, the resource links to that user's
    own resource, making it easy to figure out the user's information and
    any useful related resources.
    """
    name = 'session'
    singleton = True

    @webapi_check_login_required
    def get(self, request, *args, **kwargs):
        """Returns information on the client's session.

        This currently just contains information on the currently logged-in
        user (if any).
        """
        expanded_resources = request.GET.get('expand', '').split(',')

        authenticated = request.user.is_authenticated()

        data = {
            'authenticated': authenticated,
            'links': self.get_links(request=request),
        }

        if authenticated and 'user' in expanded_resources:
            data['user'] = request.user
            del data['links']['user']

        return 200, {
            self.name: data,
        }

    def get_related_links(self, obj=None, request=None, *args, **kwargs):
        links = {}

        if request and request.user.is_authenticated():
            user_resource = get_resource_for_object(request.user)
            href = user_resource.get_href(request.user, request,
                                          *args, **kwargs)

            links['user'] = {
                'method': 'GET',
                'href': href,
                'title': unicode(request.user),
                'resource': user_resource,
                'list-resource': False,
            }

        return links

session_resource = SessionResource()


class RootResource(DjbletsRootResource):
    """Links to all the main resources, including URI templates to resources
    anywhere in the tree.

    This should be used as a starting point for any clients that need to access
    any resources in the API. By browsing through the resource tree instead of
    hard-coding paths, your client can remain compatible with any changes in
    the resource URI scheme.
    """
    def __init__(self, *args, **kwargs):
        super(RootResource, self).__init__([
            repository_resource,
            review_group_resource,
            review_request_resource,
            server_info_resource,
            session_resource,
            user_resource,
        ], *args, **kwargs)

root_resource = RootResource()


def status_to_string(status):
    if status == "P":
        return "pending"
    elif status == "S":
        return "submitted"
    elif status == "D":
        return "discarded"
    elif status == None:
        return "all"
    else:
        raise Exception("Invalid status '%s'" % status)


def string_to_status(status):
    if status == "pending":
        return "P"
    elif status == "submitted":
        return "S"
    elif status == "discarded":
        return "D"
    elif status == "all":
        return None
    else:
        raise Exception("Invalid status '%s'" % status)


register_resource_for_model(
    Comment,
    lambda obj: obj.review.get().is_reply() and
                review_reply_diff_comment_resource or
                review_diff_comment_resource)
register_resource_for_model(DiffSet, diffset_resource)
register_resource_for_model(FileDiff, filediff_resource)
register_resource_for_model(Group, review_group_resource)
register_resource_for_model(Repository, repository_resource)
register_resource_for_model(
    Review,
    lambda obj: obj.is_reply() and review_reply_resource or review_resource)
register_resource_for_model(ReviewRequest, review_request_resource)
register_resource_for_model(ReviewRequestDraft, review_request_draft_resource)
register_resource_for_model(Screenshot, screenshot_resource)
register_resource_for_model(
    ScreenshotComment,
    lambda obj: obj.review.get().is_reply() and
                review_reply_screenshot_comment_resource or
                review_screenshot_comment_resource)
register_resource_for_model(User, user_resource)
