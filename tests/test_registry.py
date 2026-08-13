"""The registry: how models become administrable."""

import pytest
from django.test import override_settings

from tests.testapp.models import Author, Order, Post
from vantage.panels import ModelPanel
from vantage.registry import AlreadyRegistered, NotRegistered, Registry


@pytest.fixture
def registry():
    return Registry()


def test_register_a_model_with_pure_inference(registry):
    registry.register(Post)
    assert registry.is_registered(Post)
    assert isinstance(registry.get_panel(Post), ModelPanel)


def test_register_a_model_with_an_explicit_panel(registry):
    class PostPanel(ModelPanel):
        list_display = ["title"]

    registry.register(Post, PostPanel)
    assert registry.get_panel(Post).list_display == ["title"]


def test_register_decorator(registry):
    @registry.register_decorator(Post)
    class PostPanel(ModelPanel):
        list_display = ["title"]

    assert registry.get_panel(Post).list_display == ["title"]
    # The decorator returns the class unchanged so it stays importable.
    assert PostPanel.list_display == ["title"]


def test_panel_knows_its_model(registry):
    registry.register(Post)
    assert registry.get_panel(Post).model is Post


def test_registering_twice_raises(registry):
    registry.register(Post)
    with pytest.raises(AlreadyRegistered):
        registry.register(Post)


def test_unregister(registry):
    registry.register(Post)
    registry.unregister(Post)
    assert not registry.is_registered(Post)


def test_unregistering_an_unregistered_model_raises(registry):
    with pytest.raises(NotRegistered):
        registry.unregister(Post)


def test_get_panel_for_unregistered_model_raises(registry):
    with pytest.raises(NotRegistered):
        registry.get_panel(Post)


def test_registering_a_non_model_raises(registry):
    with pytest.raises(TypeError):
        registry.register(dict)


def test_registering_an_abstract_model_raises(registry):
    from django.db import models

    class Abstract(models.Model):
        class Meta:
            abstract = True
            app_label = "testapp"

    with pytest.raises(TypeError):
        registry.register(Abstract)


class TestAutoRegister:
    def test_registers_project_models(self, registry):
        registry.autoregister()
        assert registry.is_registered(Post)
        assert registry.is_registered(Author)
        assert registry.is_registered(Order)

    def test_never_clobbers_an_explicit_panel(self, registry):
        class PostPanel(ModelPanel):
            list_display = ["title"]

        registry.register(Post, PostPanel)
        registry.autoregister()
        assert registry.get_panel(Post).list_display == ["title"]

    def test_skips_framework_noise_by_default(self, registry):
        from django.contrib.contenttypes.models import ContentType
        from django.contrib.sessions.models import Session

        registry.autoregister()
        assert not registry.is_registered(Session)
        assert not registry.is_registered(ContentType)

    def test_registers_auth_models(self, registry):
        """User and Group are genuinely useful in an admin — they are not noise."""
        from django.contrib.auth.models import Group, User

        registry.autoregister()
        assert registry.is_registered(User)
        assert registry.is_registered(Group)

    @override_settings(VANTAGE={"AUTO_REGISTER": False})
    def test_disabled_by_setting(self, registry):
        registry.autoregister()
        assert not registry.is_registered(Post)

    @override_settings(VANTAGE={"EXCLUDE": ["testapp.Post"]})
    def test_exclude_a_single_model(self, registry):
        registry.autoregister()
        assert not registry.is_registered(Post)
        assert registry.is_registered(Author)

    @override_settings(VANTAGE={"EXCLUDE": ["testapp"]})
    def test_exclude_a_whole_app(self, registry):
        registry.autoregister()
        assert not registry.is_registered(Post)
        assert not registry.is_registered(Author)

    @override_settings(VANTAGE={"INCLUDE_ONLY": ["testapp.Post"]})
    def test_include_only_is_an_allowlist(self, registry):
        registry.autoregister()
        assert registry.is_registered(Post)
        assert not registry.is_registered(Author)


def test_panels_are_iterable_in_a_stable_order(registry):
    registry.autoregister()
    first = [p.model for p in registry.panels()]
    second = [p.model for p in registry.panels()]
    assert first == second
    assert Post in first
