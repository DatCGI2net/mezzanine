from unittest import skipUnless
from urllib.parse import quote_plus, urlparse

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sites.models import Site
from django.core.checks import Warning
from django.db import connection
from django.http import HttpResponse
from django.shortcuts import resolve_url
from django.template import Context, Template, TemplateSyntaxError
from django.test.utils import override_settings
from django.utils.translation import get_language

from mezzanine.conf import settings
from mezzanine.core.models import CONTENT_STATUS_PUBLISHED
from mezzanine.core.request import current_request
from mezzanine.pages.admin import PageAdminForm
from mezzanine.pages.checks import check_context_processor
from mezzanine.pages.fields import MenusField
from mezzanine.pages.models import Page, RichTextPage
from mezzanine.urls import PAGES_SLUG
from mezzanine.utils.deprecation import get_middleware_request
from mezzanine.utils.sites import override_current_site_id
from mezzanine.utils.tests import TestCase

User = get_user_model()


class PagesTests(TestCase):
    def setUp(self):
        """
        Make sure we have a thread-local request with a site_id attribute set.
        """
        super().setUp()
        from mezzanine.core.request import _thread_local

        request = self._request_factory.get("/")
        request.site_id = settings.SITE_ID
        _thread_local.request = request

    def tearDown(self):
        from mezzanine.core.request import _thread_local

        try:
            del _thread_local.request
        except AttributeError:
            pass

    def test_page_ascendants(self):
        """
        Test the methods for looking up ascendants efficiently
        behave as expected.
        """
        # Create related pages.
        primary, _ = RichTextPage.objects.get_or_create(title="Primary")
        secondary, _ = primary.children.get_or_create(title="Secondary")
        tertiary, _ = secondary.children.get_or_create(title="Tertiary")

        # Test that get_ascendants() returns the right thing.
        page = Page.objects.get(id=tertiary.id)
        ascendants = page.get_ascendants()
        self.assertEqual(ascendants[0].id, secondary.id)
        self.assertEqual(ascendants[1].id, primary.id)

        # Test ascendants are returned in order for slug, using
        # a single DB query.
        connection.queries_log.clear()
        pages_for_slug = Page.objects.with_ascendants_for_slug(tertiary.slug)
        self.assertEqual(len(connection.queries), 1)
        self.assertEqual(pages_for_slug[0].id, tertiary.id)
        self.assertEqual(pages_for_slug[1].id, secondary.id)
        self.assertEqual(pages_for_slug[2].id, primary.id)

        # Test page.get_ascendants uses the cached attribute,
        # without any more queries.
        connection.queries_log.clear()
        ascendants = pages_for_slug[0].get_ascendants()
        self.assertEqual(len(connection.queries), 0)
        self.assertEqual(ascendants[0].id, secondary.id)
        self.assertEqual(ascendants[1].id, primary.id)

        # Test with_ascendants_for_slug with invalid parent
        primary.parent_id = tertiary.id
        primary.save()
        pages_for_slug = Page.objects.with_ascendants_for_slug(tertiary.slug)
        self.assertEqual(pages_for_slug[0]._ascendants, [])
        primary.parent_id = None
        primary.save()

        # Use a custom slug in the page path, and test that
        # Page.objects.with_ascendants_for_slug fails, but
        # correctly falls back to recursive queries.
        secondary.slug += "custom"
        secondary.save()
        pages_for_slug = Page.objects.with_ascendants_for_slug(tertiary.slug)
        self.assertEqual(len(pages_for_slug[0]._ascendants), 0)
        connection.queries_log.clear()
        ascendants = pages_for_slug[0].get_ascendants()
        self.assertEqual(len(connection.queries), 2)  # 2 parent queries
        self.assertEqual(pages_for_slug[0].id, tertiary.id)
        self.assertEqual(ascendants[0].id, secondary.id)
        self.assertEqual(ascendants[1].id, primary.id)

    def test_set_parent(self):
        old_parent, _ = RichTextPage.objects.get_or_create(title="Old parent")
        new_parent, _ = RichTextPage.objects.get_or_create(title="New parent")
        child, _ = RichTextPage.objects.get_or_create(title="Child", slug="kid")
        self.assertTrue(child.parent is None)
        self.assertTrue(child.slug == "kid")

        child.set_parent(old_parent)
        child.save()
        self.assertEqual(child.parent_id, old_parent.id)
        self.assertTrue(child.slug == "old-parent/kid")

        child = RichTextPage.objects.get(id=child.id)
        self.assertEqual(child.parent_id, old_parent.id)
        self.assertTrue(child.slug == "old-parent/kid")

        child.set_parent(new_parent)
        child.save()
        self.assertEqual(child.parent_id, new_parent.id)
        self.assertTrue(child.slug == "new-parent/kid")

        child = RichTextPage.objects.get(id=child.id)
        self.assertEqual(child.parent_id, new_parent.id)
        self.assertTrue(child.slug == "new-parent/kid")

        child.set_parent(None)
        child.save()
        self.assertTrue(child.parent is None)
        self.assertTrue(child.slug == "kid")

        child = RichTextPage.objects.get(id=child.id)
        self.assertTrue(child.parent is None)
        self.assertTrue(child.slug == "kid")

        child = RichTextPage(title="child2")
        child.set_parent(new_parent)
        self.assertEqual(child.slug, "new-parent/child2")

        # Assert that cycles are detected.
        p1, _ = RichTextPage.objects.get_or_create(title="p1")
        p2, _ = RichTextPage.objects.get_or_create(title="p2")
        p2.set_parent(p1)
        with self.assertRaises(AttributeError):
            p1.set_parent(p1)
        with self.assertRaises(AttributeError):
            p1.set_parent(p2)
        p2c = RichTextPage.objects.get(title="p2")
        with self.assertRaises(AttributeError):
            p1.set_parent(p2c)

    def test_set_slug(self):
        parent, _ = RichTextPage.objects.get_or_create(title="Parent", slug="parent")
        child, _ = RichTextPage.objects.get_or_create(
            title="Child", slug="parent/child", parent_id=parent.id
        )
        parent.set_slug("new-parent-slug")
        self.assertTrue(parent.slug == "new-parent-slug")

        parent = RichTextPage.objects.get(id=parent.id)
        self.assertTrue(parent.slug == "new-parent-slug")

        child = RichTextPage.objects.get(id=child.id)
        self.assertTrue(child.slug == "new-parent-slug/child")

    def test_login_required(self):
        public, _ = RichTextPage.objects.get_or_create(
            title="Public", slug="public", login_required=False
        )
        private, _ = RichTextPage.objects.get_or_create(
            title="Private", slug="private", login_required=True
        )
        accounts_installed = "mezzanine.accounts" in settings.INSTALLED_APPS

        args = {"for_user": AnonymousUser()}
        self.assertTrue(public in RichTextPage.objects.published(**args))
        self.assertTrue(private not in RichTextPage.objects.published(**args))
        args = {"for_user": User.objects.get(username=self._username)}
        self.assertTrue(public in RichTextPage.objects.published(**args))
        self.assertTrue(private in RichTextPage.objects.published(**args))

        public_url = public.get_absolute_url()
        private_url = private.get_absolute_url()

        self.client.logout()
        response = self.client.get(private_url, follow=True)
        login_prefix = ""
        login_url = resolve_url(settings.LOGIN_URL)
        login_next = private_url
        try:
            redirects_count = len(response.redirect_chain)
            response_url = response.redirect_chain[-1][0]
        except (AttributeError, IndexError):
            redirects_count = 0
            response_url = ""
        if urlparse(response_url).path.startswith("/%s/" % get_language()):
            # With LocaleMiddleware a language code can be added at the
            # beginning of the path.
            login_prefix = "/%s" % get_language()
        if redirects_count > 1:
            # With LocaleMiddleware and a string LOGIN_URL there can be
            # a second redirect that encodes the next parameter.
            login_next = quote_plus(login_next)
        login = f"{login_prefix}{login_url}?next={login_next}"
        if accounts_installed:
            # For an inaccessible page with mezzanine.accounts we should
            # see a login page, without it 404 is more appropriate than an
            # admin login.
            target_status_code = 200
        else:
            target_status_code = 404
        self.assertRedirects(response, login, target_status_code=target_status_code)
        response = self.client.get(public_url, follow=True)
        self.assertEqual(response.status_code, 200)

        if accounts_installed:
            # View / pattern name redirect properly, without encoding next.
            login = f"{login_prefix}{login_url}?next={private_url}"
            with override_settings(LOGIN_URL="login"):
                # Note: The "login" is a pattern name in accounts.urls.
                response = self.client.get(public_url, follow=True)
                self.assertEqual(response.status_code, 200)
                response = self.client.get(private_url, follow=True)
                self.assertRedirects(response, login)

        self.client.login(username=self._username, password=self._password)
        response = self.client.get(private_url, follow=True)
        self.assertEqual(response.status_code, 200)
        response = self.client.get(public_url, follow=True)
        self.assertEqual(response.status_code, 200)

        if accounts_installed:
            with override_settings(LOGIN_URL="mezzanine.accounts.views.login"):
                response = self.client.get(public_url, follow=True)
                self.assertEqual(response.status_code, 200)
                response = self.client.get(private_url, follow=True)
                self.assertEqual(response.status_code, 200)
            with override_settings(LOGIN_URL="login"):
                response = self.client.get(public_url, follow=True)
                self.assertEqual(response.status_code, 200)
                response = self.client.get(private_url, follow=True)
                self.assertEqual(response.status_code, 200)

    def test_set_model_permissions(self):
        from mezzanine.core.request import _thread_local

        template = (
            "{% load pages_tags %}" "{% set_model_permissions model %}{{ model.perms }}"
        )
        request = _thread_local.request
        request.user = AnonymousUser()
        rendered = Template(template).render(
            Context({"model": RichTextPage, "request": request})
        )
        self.assertIsNotNone(rendered)

    def test_set_page_permissions(self):
        from mezzanine.core.request import _thread_local

        template = (
            "{% load pages_tags %}" "{% set_page_permissions page %}{{ page.perms }}"
        )
        request = _thread_local.request
        request.user = AnonymousUser()
        home_page, _ = RichTextPage.objects.get_or_create(slug="/", title="home")
        rendered = Template(template).render(
            Context({"page": home_page, "request": request})
        )
        self.assertIsNotNone(rendered)

    def test_page_menu_key_error(self):
        """
        Test that rendering a page menu without a template name or
        context["menu_template_name"] raises a TemplateSystemError.
        """
        template = "{% load pages_tags %}" "{% page_menu %}"
        with self.assertRaises(TemplateSyntaxError):
            Template(template).render(Context({}))

    def test_page_menu_slug_home(self):
        from mezzanine.core.request import _thread_local

        home, _ = RichTextPage.objects.get_or_create(slug="/", title="home")
        template = "{% load pages_tags %}" '{% page_menu "pages/menus/tree.html" %}'
        request = _thread_local.request
        request.user = AnonymousUser()
        rendered = Template(template).render(
            Context({"page": home, "request": request})
        )
        self.assertIsNotNone(rendered)

    def test_page_menu_queries(self):
        """
        Test that rendering a page menu executes the same number of
        queries regardless of the number of pages or levels of
        children.
        """
        template = "{% load pages_tags %}" '{% page_menu "pages/menus/tree.html" %}'
        before = self.queries_used_for_template(template)
        self.assertTrue(before > 0)
        self.create_recursive_objects(
            RichTextPage, "parent", title="Page", status=CONTENT_STATUS_PUBLISHED
        )
        after = self.queries_used_for_template(template)
        self.assertEqual(before, after)

    def test_page_menu_flags(self):
        """
        Test that pages only appear in the menu templates they've been
        assigned to show in.
        """
        menus = []
        pages = []
        template = "{% load pages_tags %}"
        for i, label, path in settings.PAGE_MENU_TEMPLATES:
            menus.append(i)
            pages.append(
                RichTextPage.objects.create(
                    in_menus=list(menus),
                    title="Page for %s" % str(label),
                    status=CONTENT_STATUS_PUBLISHED,
                )
            )
            template += "{%% page_menu '%s' %%}" % path
        rendered = Template(template).render(Context({}))
        for page in pages:
            self.assertEqual(rendered.count(page.title), len(page.in_menus))

    def test_page_menu_default(self):
        """
        Test that the settings-defined default value for the ``in_menus``
        field is used, also checking that it doesn't get forced to text,
        but that sequences are made immutable.
        """
        with override_settings(PAGE_MENU_TEMPLATES=((8, "a", "a"), (9, "b", "b"))):
            with override_settings(PAGE_MENU_TEMPLATES_DEFAULT=None):
                page_in_all_menus = Page.objects.create()
                self.assertEqual(page_in_all_menus.in_menus, (8, 9))
            with override_settings(PAGE_MENU_TEMPLATES_DEFAULT=tuple()):
                page_not_in_menus = Page.objects.create()
                self.assertEqual(page_not_in_menus.in_menus, tuple())
            with override_settings(PAGE_MENU_TEMPLATES_DEFAULT=[9]):
                page_in_a_menu = Page.objects.create()
                self.assertEqual(page_in_a_menu.in_menus, (9,))

    def test_menusfield_default(self):
        my_default = (1, 3)

        def my_default_func():
            return my_default

        choices = (
            (1, "First Menu", "template1"),
            (2, "Second Menu", "template2"),
            (3, "Third Menu", "template3"),
        )
        with override_settings(PAGE_MENU_TEMPLATES=choices):
            with override_settings(PAGE_MENU_TEMPLATES_DEFAULT=(1, 2, 3)):
                # test default
                field = MenusField(choices=choices, default=my_default)
                self.assertTrue(field.has_default())
                self.assertEqual(my_default, field.get_default())
                # test callable default
                field = MenusField(choices=choices, default=my_default_func)
                self.assertTrue(field.has_default())
                self.assertEqual(my_default, field.get_default())

    def test_overridden_page(self):
        """
        Test that a page with a slug matching a non-page urlpattern
        return ``True`` for its overridden property.
        """
        # BLOG_SLUG is empty then urlpatterns for pages are prefixed
        # with PAGE_SLUG, and generally won't be overridden. In this
        # case, there aren't any overridding URLs by default, so bail
        # on the test.
        if PAGES_SLUG:
            return
        page, _ = RichTextPage.objects.get_or_create(slug="edit")
        self.assertTrue(page.overridden())

    def test_unicode_slug_parm_to_processor_for(self):
        """
        Test that passing an unicode slug to processor_for works for
        python 2.x
        """
        from mezzanine.pages.page_processors import processor_for

        @processor_for("test unicode string")
        def test_page_processor(request, page):
            return {}

        page, _ = RichTextPage.objects.get_or_create(title="test page")
        self.assertEqual(test_page_processor(current_request(), page), {})

    def test_exact_page_processor_for(self):
        """
        Test that passing exact_page=True works with the PageMiddleware
        """
        from mezzanine.pages.middleware import PageMiddleware
        from mezzanine.pages.page_processors import processor_for
        from mezzanine.pages.views import page as page_view

        @processor_for("foo/bar", exact_page=True)
        def test_page_processor(request, page):
            return HttpResponse("bar")

        foo, _ = RichTextPage.objects.get_or_create(title="foo")
        bar, _ = RichTextPage.objects.get_or_create(title="bar", parent=foo)

        request = self._request_factory.get("/foo/bar/")
        request.user = self._user

        response = PageMiddleware(get_middleware_request).process_view(
            request, page_view, [], {}
        )

        self.assertTrue(isinstance(response, HttpResponse))
        self.assertContains(response, "bar")

    @skipUnless(
        settings.USE_MODELTRANSLATION and len(settings.LANGUAGES) > 1,
        "modeltranslation configured for several languages required",
    )
    def test_page_slug_has_correct_lang(self):
        """
        Test that slug generation is done for the default language and
        not the active one.
        """
        from collections import OrderedDict

        from django.utils.translation import activate, get_language

        from mezzanine.utils.urls import slugify

        default_language = get_language()
        code_list = OrderedDict(settings.LANGUAGES)
        del code_list[default_language]
        title_1 = "Title firt language"
        title_2 = "Title second language"
        page, _ = RichTextPage.objects.get_or_create(title=title_1)
        for code in code_list:
            try:
                activate(code)
            except:  # noqa
                pass
            else:
                break
            # No valid language found
            page.delete()
            return
        page.title = title_2
        page.save()
        self.assertEqual(page.get_slug(), slugify(title_1))
        self.assertEqual(page.title, title_2)
        activate(default_language)
        self.assertEqual(page.title, title_1)
        page.delete()

    def test_clean_slug(self):
        """
        Test that PageAdminForm strips leading and trailing slashes
        from slugs or returns `/`.
        """

        class TestPageAdminForm(PageAdminForm):
            class Meta:
                fields = ["slug"]
                model = Page

        data = {"slug": "/"}
        submitted_form = TestPageAdminForm(data=data)
        self.assertTrue(submitted_form.is_valid())
        self.assertEqual(submitted_form.cleaned_data["slug"], "/")

        data = {"slug": "/hello/world/"}
        submitted_form = TestPageAdminForm(data=data)
        self.assertTrue(submitted_form.is_valid())
        self.assertEqual(submitted_form.cleaned_data["slug"], "hello/world")

    def test_ascendants_different_site(self):
        site2 = Site.objects.create(domain="site2.example.com", name="Site 2")

        parent = Page.objects.create(title="Parent", site=site2)
        child = parent.children.create(title="Child", site=site2)
        grandchild = child.children.create(title="Grandchild", site=site2)

        # Re-retrieve grandchild so its parent attribute is not cached
        with override_current_site_id(site2.id):
            grandchild = Page.objects.get(pk=grandchild.pk)

        with self.assertNumQueries(1):
            self.assertListEqual(grandchild.get_ascendants(), [child, parent])

    def test_check_context_processor(self):
        context_processor = "mezzanine.pages.context_processors.page"
        templates = [{"OPTIONS": {"context_processors": context_processor}}]
        expected_warning = [
            Warning(
                "You haven't included 'mezzanine.pages.context_processors.page' "
                "as a context processor in any of your template configurations. "
                "Your templates might not work as expected.",
                id="mezzanine.pages.W01",
            )
        ]
        app_config = apps.get_app_config("pages")
        with override_settings(TEMPLATES=()):
            issues = check_context_processor(app_config)
            self.assertEqual(issues, expected_warning)
        with override_settings(TEMPLATES=templates):
            issues = check_context_processor(app_config)
            self.assertEqual(issues, [])
