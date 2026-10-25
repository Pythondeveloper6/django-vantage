"""The relationship map."""

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from tests.testapp.models import Post
from vantage.erd import NODE_WIDTH, Edge, Node, _assemble, build_graph
from vantage.sites import site


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


class TestGraph:
    def test_every_registered_model_is_a_node(self):
        keys = {n.key for n in build_graph(site).nodes}
        assert "testapp.post" in keys
        assert "testapp.author" in keys

    def test_foreign_keys_become_edges(self):
        edges = build_graph(site).edges
        assert any(
            e.source == "testapp.post" and e.target == "testapp.author" and e.kind == "fk"
            for e in edges
        )

    def test_many_to_many_is_marked_as_such(self):
        edges = build_graph(site).edges
        assert any(
            e.source == "testapp.post" and e.target == "testapp.tag" and e.kind == "m2m"
            for e in edges
        )

    def test_a_self_reference_is_flagged(self):
        edge = next(e for e in build_graph(site).edges if e.source == "testapp.category")
        assert edge.self_referential is True

    def test_an_edge_carries_the_field_name(self):
        edge = next(
            e
            for e in build_graph(site).edges
            if e.source == "testapp.post" and e.target == "testapp.author"
        )
        assert edge.label == "author"

    def test_nodes_link_to_their_list_page(self):
        node = next(n for n in build_graph(site).nodes if n.key == "testapp.post")
        assert node.url == reverse("vantage:testapp-post-list")


class TestLayout:
    def test_every_node_gets_a_position(self):
        assert all(n.x >= 0 and n.y >= 0 for n in build_graph(site).nodes)

    def test_nodes_in_a_layer_share_a_column(self):
        graph = build_graph(site)
        by_layer: dict[int, set[int]] = {}
        for node in graph.nodes:
            by_layer.setdefault(node.layer, set()).add(node.x)
        assert all(len(xs) == 1 for xs in by_layer.values())

    def test_layers_advance_left_to_right(self):
        graph = build_graph(site)
        positions = {n.layer: n.x for n in graph.nodes}
        ordered = [positions[layer] for layer in sorted(positions)]
        assert ordered == sorted(ordered)
        if len(ordered) > 1:
            assert ordered[1] - ordered[0] >= NODE_WIDTH

    def test_nodes_within_a_column_do_not_overlap(self):
        graph = build_graph(site)
        by_layer: dict[int, list[int]] = {}
        for node in graph.nodes:
            by_layer.setdefault(node.layer, []).append(node.y)
        for ys in by_layer.values():
            assert len(set(ys)) == len(ys)

    def test_the_canvas_contains_every_node(self):
        graph = build_graph(site)
        assert all(n.x + NODE_WIDTH <= graph.width for n in graph.nodes)

    def test_a_dependency_cycle_still_lays_out(self):
        """Mutually-referencing models must not stall the layering loop."""
        graph = build_graph(site)
        assert len(graph.nodes) == len({n.key for n in graph.nodes})

    def test_every_edge_has_a_drawable_path(self):
        assert all(e.path.startswith("M ") for e in build_graph(site).edges)

    def test_a_self_reference_draws_a_loop_not_a_zero_length_line(self):
        edge = next(e for e in build_graph(site).edges if e.self_referential)
        assert "C" in edge.path

    def test_a_self_reference_loop_on_the_top_row_stays_on_the_canvas(self):
        # A curve never leaves the hull of its control points, so keeping every point
        # inside the canvas keeps the whole loop visible.
        node = Node(key="shop.category", label="Categories", url=None, count_fields=3)
        edge = Edge(
            source=node.key, target=node.key, label="parent", kind="fk", self_referential=True
        )
        graph = _assemble({node.key: node}, [edge])
        numbers = [float(n) for n in edge.path.replace(",", " ").split() if n not in "MC"]
        ys = numbers[1::2]
        assert min(ys) >= 0, edge.path
        assert max(ys) <= graph.height, edge.path


class TestPermissions:
    def test_a_hidden_model_is_not_a_node(self, rf, db):
        request = rf.get("/")
        staff = User.objects.create_user("plain", password="x", is_staff=True)
        ct = ContentType.objects.get_for_model(Post)
        staff.user_permissions.add(Permission.objects.get(content_type=ct, codename="view_post"))
        request.user = User.objects.get(pk=staff.pk)

        keys = {n.key for n in build_graph(site, request).nodes}
        assert keys == {"testapp.post"}

    def test_an_edge_to_a_hidden_model_is_dropped(self, rf, db):
        """A dangling line would still disclose that the relation exists."""
        request = rf.get("/")
        staff = User.objects.create_user("plain2", password="x", is_staff=True)
        ct = ContentType.objects.get_for_model(Post)
        staff.user_permissions.add(Permission.objects.get(content_type=ct, codename="view_post"))
        request.user = User.objects.get(pk=staff.pk)

        assert build_graph(site, request).edges == []


class TestPage:
    def test_it_renders(self, boss):
        assert boss.get(reverse("vantage:page-schema")).status_code == 200

    def test_it_draws_an_svg(self, boss):
        body = boss.get(reverse("vantage:page-schema")).content.decode()
        assert "<svg" in body
        assert 'role="img"' in body

    def test_nodes_are_links(self, boss):
        body = boss.get(reverse("vantage:page-schema")).content.decode()
        assert reverse("vantage:testapp-post-list") in body

    def test_it_appears_in_the_sidebar(self, boss):
        body = boss.get(reverse("vantage:dashboard")).content.decode()
        assert reverse("vantage:page-schema") in body

    def test_it_needs_no_javascript(self, boss):
        """Layout is computed server-side, so the diagram cannot flash or fail to load."""
        body = boss.get(reverse("vantage:page-schema")).content.decode()
        svg = body[body.index("<svg") : body.index("</svg>")]
        assert "<script" not in svg
