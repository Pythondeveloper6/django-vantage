"""The screens behind :class:`vantage.pages.RolesPage`.

Thin on purpose: every decision about who may change what, and what a change actually
does, is made in :mod:`vantage.roles`. What this module owns is the HTTP shape, and two
choices in it are worth stating.

**Every write is a real form POST that redirects.** The matrix's review step and its
"+12 / -3" counter are JavaScript, but the save underneath them is the same POST a page
with no script sends — one request, one transaction, Post/Redirect/Get afterwards. A
separate JSON endpoint for the enhanced path would be a second write path whose
permission checks had to be kept identical to the first, which is how one of them
stops being.

**CSRF is enforced here, not assumed.** Each view is wrapped in ``csrf_protect``, so
the screen that grants permissions stays protected in a project that removed
``CsrfViewMiddleware`` for an API, or mounted the panel behind a decorator that exempts
it. On every other screen the middleware is enough; on this one, defence in depth is
cheap and the failure it guards against is a forged grant.
"""

from __future__ import annotations

from functools import cached_property

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_protect

from vantage import roles
from vantage.models import VantageLogEntry
from vantage.permissions import get_permissions
from vantage.safety import read_only_reason
from vantage.views import SiteView

__all__ = [
    "MEMBERS_SHOWN",
    "RoleCreateView",
    "RoleDeleteView",
    "RoleDuplicateView",
    "RoleEditView",
    "RoleMembersView",
    "RoleRenameView",
    "RoleUserSearchView",
    "RolesView",
    "UserPermissionsView",
    "build_urls",
    "list_context",
]

#: Members listed on the members tab before it asks for a search instead. A role held
#: by twenty thousand accounts is real, and rendering them all is not a page.
MEMBERS_SHOWN = 200


def _may(request, action: str) -> bool:
    """The panel's own answer for ``auth.Group``, which already folds in read-only
    mode and the superuser bypass — so this screen cannot disagree with the rest of
    the panel about whether writes are allowed right now."""
    return get_permissions().has_model_perm(request, Group, action)


def list_context(request) -> dict:
    """What the roles list renders. Called by :class:`~vantage.pages.RolesPage`."""
    return {
        "roles": list(roles.role_rows()),
        "can_add": _may(request, "add"),
        "can_change": _may(request, "change"),
        "can_delete": _may(request, "delete"),
        "membership": roles.supports_membership(),
        "name_limit": roles.MAX_NAME_LENGTH,
    }


@method_decorator(csrf_protect, name="dispatch")
class RolesView(SiteView):
    """Shared gate, lookups and refusals for every roles screen."""

    def check_permission(self, request):
        if roles.may_manage_roles(request):
            return None
        raise PermissionDenied(_("You do not have permission to manage roles."))

    # ── lookups ─────────────────────────────────────────────────────────────

    @cached_property
    def group(self) -> Group:
        return get_object_or_404(Group, pk=self.kwargs["pk"])

    @cached_property
    def reach(self) -> roles.Reach:
        return roles.reach_for(self.request.user, self.request)

    @cached_property
    def group_permissions(self) -> list:
        return list(self.group.permissions.select_related("content_type"))

    @cached_property
    def field_rules(self) -> dict:
        return roles.current_field_rules(self.group)

    @cached_property
    def within_reach(self) -> bool:
        """Whether the actor holds every permission this role grants.

        Required for anything that hands the role's whole bundle to someone — adding a
        member, duplicating it — or that removes it from people, and for renaming and
        deleting, which change what everyone holding it can do.
        """
        return self.reach.covers_all(self.group_permissions)

    # ── refusals ────────────────────────────────────────────────────────────

    def require(self, action: str) -> None:
        if _may(self.request, action):
            return
        reason = read_only_reason()
        raise PermissionDenied(reason or _("You do not have permission to change roles."))

    def require_reach(self) -> None:
        if not self.within_reach:
            raise PermissionDenied(
                _(
                    "This role grants permissions you do not hold yourself, so you "
                    "cannot change who has it, rename it, copy it or delete it."
                )
            )

    def require_not_own(self) -> None:
        """Refuse a non-superuser changing a role they hold themselves.

        Field rules combine most-permissively and a role's grants are its members'
        access, so editing, renaming or deleting a role you hold is editing your own
        access — the one thing a delegated roles screen must not allow. A superuser is
        unrestricted anyway, so the rule would protect nothing for them.
        """
        user = self.request.user
        if getattr(user, "is_superuser", False):
            return
        if user.groups.filter(pk=self.group.pk).exists():
            raise PermissionDenied(
                _("You hold this role yourself, so someone else has to change it.")
            )

    # ── shared rendering ────────────────────────────────────────────────────

    def list_url(self) -> str:
        return reverse("vantage:page-roles")

    def get_breadcrumbs(self):
        crumbs = [(gettext("Roles"), self.list_url())]
        if "pk" in self.kwargs:
            crumbs.append((self.group.name, None))
        return crumbs

    def role_context(self, tab: str) -> dict:
        group = self.group
        token = roles.fingerprint(group, {p.pk for p in self.group_permissions}, self.field_rules)
        return {
            "group": group,
            "tab": tab,
            "token": token,
            "within_reach": self.within_reach,
            "can_change": _may(self.request, "change"),
            "can_add": _may(self.request, "add"),
            "can_delete": _may(self.request, "delete"),
            "membership": roles.supports_membership(),
            "name_limit": roles.MAX_NAME_LENGTH,
        }


# ── the list's one write ────────────────────────────────────────────────────


class RoleCreateView(RolesView):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        self.require("add")
        try:
            group = roles.create_role(request.POST.get("name", ""))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return HttpResponseRedirect(self.list_url())
        _audit(request, VantageLogEntry.Action.CREATE, group, {"name": {"to": group.name}})
        messages.success(request, _("Role “%(name)s” created.") % {"name": group.name})
        return HttpResponseRedirect(reverse("vantage:role-edit", args=[group.pk]))


# ── the matrix ──────────────────────────────────────────────────────────────


class RoleEditView(RolesView):
    template_name = "vantage/roles/edit.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current = {p.pk for p in self.group_permissions}
        catalogue = kwargs.get("catalogue") or roles.load_catalogue()
        displayed = kwargs.get("displayed", current)
        base_rules = self.field_rules
        context.update(self.role_context("permissions"))
        context.update(
            page_title=self.group.name,
            page_description=_("Tick what this role may do. Nothing is saved until you review it."),
            sections=roles.build_matrix(
                self.site,
                catalogue,
                displayed,
                self.reach,
                group=self.group,
                rules=kwargs.get("displayed_rules", base_rules),
                base_rules=base_rules,
            ),
            base_ids=",".join(str(pk) for pk in sorted(current)),
            base_rules=",".join(
                f"{ct}:{name}:{level}" for (ct, name), level in sorted(base_rules.items())
            ),
            base=current,
            actions=roles.ACTIONS,
            partial_reach=not self.reach.everything,
            conflict=kwargs.get("conflict"),
            has_unregistered=False,
        )
        context["has_unregistered"] = any(s.unregistered for s in context["sections"])
        return context

    def post(self, request, *args, **kwargs):
        self.require("change")
        self.require_not_own()
        group = self.group
        catalogue = roles.load_catalogue()
        add, remove = self._intent(request)
        fields = self._field_intent(request)
        try:
            change = roles.save_permissions(
                group,
                token=request.POST.get("token", ""),
                add=add,
                remove=remove,
                reach=self.reach,
                catalogue=catalogue,
                fields=fields,
                site=self.site,
            )
        except Group.DoesNotExist:
            messages.error(request, _("That role was deleted by someone else."))
            return HttpResponseRedirect(self.list_url())
        except roles.RoleConflict as conflict:
            return self._conflict(request, conflict, catalogue, add, remove, fields)

        roles.audit_permissions(request, group, change)
        if change:
            messages.success(
                request,
                _("Saved “%(name)s”: %(added)d added, %(removed)d removed.")
                % {"name": group.name, "added": len(change.added), "removed": len(change.removed)},
            )
            if change.fields:
                messages.success(
                    request,
                    ngettext(
                        "%(count)d field rule changed.",
                        "%(count)d field rules changed.",
                        len(change.fields),
                    )
                    % {"count": len(change.fields)},
                )
        else:
            messages.info(request, _("Nothing to save — the role already matched."))
        if change.ignored:
            messages.warning(
                request,
                ngettext(
                    "%(count)d requested change was ignored: an unknown permission, or "
                    "one you do not hold yourself.",
                    "%(count)d requested changes were ignored: unknown permissions, or "
                    "ones you do not hold yourself.",
                    change.ignored,
                )
                % {"count": change.ignored},
            )
        return HttpResponseRedirect(reverse("vantage:role-edit", args=[group.pk]))

    @staticmethod
    def _intent(request) -> tuple[set[int], set[int]]:
        """What the actor asked for, as ``(add, remove)``.

        The enhanced page posts the reviewed diff directly. Without JavaScript the
        form posts every ticked box, and the diff is taken against the ``base`` the
        page was rendered from — never against the database, which may have moved:
        the actor's intent is what *they* changed, and the token decides whether that
        intent still applies.
        """
        if request.POST.get("mode") == "diff":
            return (
                roles.parse_ids(request.POST.getlist("add")),
                roles.parse_ids(request.POST.getlist("remove")),
            )
        base = roles.parse_ids(request.POST.getlist("base"))
        ticked = roles.parse_ids(request.POST.getlist("perm"))
        return ticked - base, base - ticked

    @staticmethod
    def _field_intent(request) -> dict:
        """The field rules the actor changed, as ``{(content type id, field): level}``.

        Same two shapes as :meth:`_intent`. The enhanced page posts only the rules that
        moved, in ``fields``. Without JavaScript every rule's ``<select>`` arrives as a
        ``field`` value, and the diff is taken against ``field_base`` — the rules as the
        page was rendered, not as the database holds them now.
        """
        if request.POST.get("mode") == "diff":
            return roles.parse_field_rules(request.POST.getlist("fields"))
        submitted = roles.parse_field_rules(request.POST.getlist("field"))
        base = roles.parse_field_rules(request.POST.getlist("field_base"))
        return {key: level for key, level in submitted.items() if base.get(key, "") != level}

    def _conflict(self, request, conflict, catalogue, add, remove, fields=None):
        """409, with the other person's change shown and this person's re-applied.

        Rendered rather than redirected, because the actor's pending edits live only
        in this request and a redirect would throw them away.
        """
        self.group = conflict.group
        current = conflict.permission_ids
        self.group_permissions = [catalogue.by_id[pk] for pk in current if pk in catalogue.by_id]
        self.field_rules = conflict.rules
        base = roles.parse_ids(request.POST.getlist("base"))

        def labels(ids):
            return sorted(
                roles.perm_label(catalogue.by_id[pk]) for pk in ids if pk in catalogue.by_id
            )

        displayed = set(current)
        for pk in add:
            permission = catalogue.get(pk)
            if permission is not None and self.reach.covers(permission):
                displayed.add(pk)
        for pk in remove:
            permission = catalogue.get(pk)
            if permission is not None and self.reach.covers(permission):
                displayed.discard(pk)

        # The actor's field changes re-applied on top of theirs, within reach, exactly
        # like the permission ids above.
        displayed_rules = dict(conflict.rules)
        for key, level in roles._valid_field_rules(fields or {}, self.site, self.reach).items():
            if level:
                displayed_rules[key] = level
            else:
                displayed_rules.pop(key, None)

        context = self.get_context_data(
            catalogue=catalogue,
            displayed=displayed,
            displayed_rules=displayed_rules,
            conflict={
                "added": labels(current - base),
                "removed": labels(base - current),
            },
        )
        return self.render_to_response(context, status=409)


class RoleRenameView(RolesView):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        self.require("change")
        self.require_reach()
        self.require_not_own()
        group = self.group
        target = reverse("vantage:role-edit", args=[group.pk])
        try:
            old, new = roles.rename_role(
                group, request.POST.get("name", ""), token=request.POST.get("token", "")
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return HttpResponseRedirect(target)
        except Group.DoesNotExist:
            messages.error(request, _("That role was deleted by someone else."))
            return HttpResponseRedirect(self.list_url())
        except roles.RoleConflict:
            messages.error(
                request,
                _(
                    "Someone else changed this role while you were looking. "
                    "Check it, then rename it."
                ),
            )
            return HttpResponseRedirect(target)
        if old != new:
            _audit(
                request, VantageLogEntry.Action.UPDATE, group, {"name": {"from": old, "to": new}}
            )
            messages.success(request, _("Renamed to “%(name)s”.") % {"name": new})
        return HttpResponseRedirect(target)


class RoleDuplicateView(RolesView):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        self.require("add")
        self.require_reach()
        original = self.group
        copy, ids = roles.duplicate_role(original)
        by_id = {p.pk: p for p in self.group_permissions}
        changes = {
            "name": {"to": copy.name},
            gettext("Copied from"): {"to": original.name},
        }
        if ids:
            changes[gettext("Permissions added")] = {
                "to": ", ".join(sorted(roles.perm_label(by_id[pk]) for pk in ids if pk in by_id))
            }
        _audit(request, VantageLogEntry.Action.CREATE, copy, changes)
        messages.success(
            request,
            _(
                "Created “%(copy)s” with the same permissions as “%(name)s”. "
                "Members were not copied."
            )
            % {"copy": copy.name, "name": original.name},
        )
        return HttpResponseRedirect(reverse("vantage:role-edit", args=[copy.pk]))


class RoleDeleteView(RolesView):
    template_name = "vantage/roles/delete.html"

    def get(self, request, *args, **kwargs):
        self.require("delete")
        self.require_reach()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.role_context("delete"))
        context.update(
            page_title=gettext("Delete “%(name)s”?") % {"name": self.group.name},
            impact=roles.delete_impact(self.group),
        )
        return context

    def post(self, request, *args, **kwargs):
        self.require("delete")
        self.require_reach()
        self.require_not_own()
        group = self.group
        labels = sorted(roles.perm_label(p) for p in self.group_permissions)
        members = list(
            roles.members_of(group).values_list(get_user_model().USERNAME_FIELD, flat=True)
        )
        try:
            ghost = roles.delete_role(group, token=request.POST.get("token", ""))
        except Group.DoesNotExist:
            messages.info(request, _("That role was already deleted."))
            return HttpResponseRedirect(self.list_url())
        except roles.RoleConflict:
            messages.error(
                request,
                _("Someone else changed this role while you were looking. Check the impact again."),
            )
            return HttpResponseRedirect(reverse("vantage:role-delete", args=[group.pk]))
        changes = {"name": {"from": ghost.name}}
        if labels:
            changes[gettext("Permissions removed")] = {"from": ", ".join(labels)}
        if members:
            changes[gettext("Members removed")] = {"from": ", ".join(str(m) for m in members)}
        _audit(request, VantageLogEntry.Action.DELETE, ghost, changes)
        messages.success(request, _("Role “%(name)s” deleted.") % {"name": ghost.name})
        return HttpResponseRedirect(self.list_url())


# ── members ─────────────────────────────────────────────────────────────────


class RoleMembersView(RolesView):
    template_name = "vantage/roles/members.html"

    def get(self, request, *args, **kwargs):
        if not roles.supports_membership():
            raise Http404(_("This project's user model has no roles."))
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.role_context("members"))
        term = self.request.GET.get("q", "").strip()
        members = roles.members_of(self.group)
        total = members.count() if roles.supports_membership() else 0
        if term:
            members = members.filter(_user_search(term))
        shown = list(members[: MEMBERS_SHOWN + 1])
        user_model = get_user_model()
        context.update(
            page_title=self.group.name,
            page_description=_("The people who hold this role."),
            members=[_user_row(u) for u in shown[:MEMBERS_SHOWN]],
            members_more=len(shown) > MEMBERS_SHOWN,
            members_total=total,
            term=term,
            user_model_label=user_model._meta.label,
            search_url=reverse("vantage:role-users"),
            candidates=[
                _user_row(u)
                for u in user_model._default_manager.exclude(
                    pk__in=roles.members_of(self.group).values("pk")
                ).order_by(user_model.USERNAME_FIELD)[:20]
            ]
            if roles.supports_membership()
            else [],
        )
        return context

    def post(self, request, *args, **kwargs):
        self.require("change")
        self.require_reach()
        if not roles.supports_membership():
            raise Http404(_("This project's user model has no roles."))
        group = self.group
        add = set(request.POST.getlist("add"))
        remove = set(request.POST.getlist("remove"))
        if not getattr(request.user, "is_superuser", False):
            # Joining a role is the shortest route to lifting your own field rules, and
            # leaving one is changing your own access too. Someone else does it.
            if str(request.user.pk) in add | remove:
                raise PermissionDenied(
                    _("You cannot add yourself to a role or remove yourself from one.")
                )
        joining, leaving = roles.change_members(group, add=add, remove=remove)
        roles.audit_members(request, group, joining, leaving)
        if joining or leaving:
            messages.success(
                request,
                _("Members updated: %(added)d added, %(removed)d removed.")
                % {"added": len(joining), "removed": len(leaving)},
            )
        else:
            messages.info(request, _("Nobody was added or removed."))
        return HttpResponseRedirect(reverse("vantage:role-members", args=[group.pk]))


def _user_search(term: str) -> Q:
    """Username, email and names — whichever of them the user model actually has."""
    user_model = get_user_model()
    names = {
        user_model.USERNAME_FIELD,
        user_model.get_email_field_name(),
        "first_name",
        "last_name",
    }
    concrete = {f.name for f in user_model._meta.concrete_fields}
    combined = Q()
    for name in sorted(names & concrete):
        combined |= Q(**{f"{name}__icontains": term})
    return combined


def _user_label(user) -> str:
    full = user.get_full_name() if hasattr(user, "get_full_name") else ""
    username = user.get_username()
    return f"{username} — {full}" if full and full != username else username


def _user_row(user) -> dict:
    """What the screens show about a user. Never ``is_staff`` or ``is_superuser``:
    the roles screen may be open to non-superusers, and who holds the keys to the
    whole panel is not theirs to browse."""
    email_field = get_user_model().get_email_field_name()
    return {
        "pk": user.pk,
        "username": user.get_username(),
        "name": user.get_full_name() if hasattr(user, "get_full_name") else "",
        "email": getattr(user, email_field, "") or "",
        "active": getattr(user, "is_active", True),
        "label": _user_label(user),
    }


class RoleUserSearchView(RolesView):
    """The member picker's endpoint, in the shape :mod:`vantage.autocomplete` serves.

    Its own route rather than the shared autocomplete endpoint, because that one only
    answers for models this panel registers and checks ``view`` on them — and a project
    may register neither ``auth.User`` nor anything that grants viewing it, while still
    wanting to manage who holds a role.
    """

    def get(self, request, *args, **kwargs):
        from vantage.autocomplete import options_response

        user_model = get_user_model()
        term = request.GET.get("q", "").strip()
        queryset = user_model._default_manager.order_by(user_model.USERNAME_FIELD)
        if term:
            queryset = queryset.filter(_user_search(term))
        return options_response(queryset, term, label=_user_label)


# ── one user's effective permissions ────────────────────────────────────────


class UserPermissionsView(RolesView):
    template_name = "vantage/roles/user.html"

    @cached_property
    def target(self):
        user_model = get_user_model()
        try:
            pk = user_model._meta.pk.to_python(self.kwargs["user"])
        except ValidationError:
            raise Http404 from None
        return get_object_or_404(user_model, pk=pk)

    def get_breadcrumbs(self):
        return [(gettext("Roles"), self.list_url()), (self.target.get_username(), None)]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        target = self.target
        membership = roles.supports_membership()
        groups = list(roles.roles_of(target)) if membership else []
        rows = roles.effective_permissions(target)
        context.update(
            page_title=gettext("Permissions of %(user)s") % {"user": target.get_username()},
            page_description=_(
                "Everything this account holds, and where each permission comes from."
            ),
            target=_user_row(target),
            target_roles=groups,
            effective=rows,
            effective_count=sum(len(items) for _label, items in rows),
            field_restrictions=roles.effective_field_rules(target, self.site),
            # Only a superuser is told that the target is one: see _user_row.
            target_is_superuser=bool(
                getattr(self.request.user, "is_superuser", False)
                and getattr(target, "is_superuser", False)
            ),
        )
        return context


# ── routes ──────────────────────────────────────────────────────────────────


def _audit(request, action, group, changes) -> None:
    from vantage.audit import record

    record(request, action, obj=group, changes=changes, action_name="role")


def build_urls(site) -> list:
    """The editor, members and per-user screens, under the roles page's own path.

    Empty when no :class:`~vantage.pages.RolesPage` is registered, so a project that
    removes the page removes every write route with it rather than leaving them
    reachable by URL.
    """
    from vantage.pages import RolesPage

    page = next((p for p in site.pages() if issubclass(p, RolesPage)), None)
    if page is None:
        return []
    base = page.get_path()
    return [
        path(f"{base}new/", RoleCreateView.as_view(site=site), name="role-create"),
        path(f"{base}users.json", RoleUserSearchView.as_view(site=site), name="role-users"),
        path(f"{base}users/<str:user>/", UserPermissionsView.as_view(site=site), name="role-user"),
        path(f"{base}<int:pk>/", RoleEditView.as_view(site=site), name="role-edit"),
        path(f"{base}<int:pk>/rename/", RoleRenameView.as_view(site=site), name="role-rename"),
        path(
            f"{base}<int:pk>/duplicate/",
            RoleDuplicateView.as_view(site=site),
            name="role-duplicate",
        ),
        path(f"{base}<int:pk>/delete/", RoleDeleteView.as_view(site=site), name="role-delete"),
        path(f"{base}<int:pk>/members/", RoleMembersView.as_view(site=site), name="role-members"),
    ]
