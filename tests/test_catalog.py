import pytest
from schemagate import Catalog, HashingEmbedder, Principal

def test_reflects_everything(cat):
    assert len(cat._docs) == 42
    kinds = {d.kind for d in cat._docs.values()}
    assert kinds == {"TABLE", "VIEW"}

def test_reflects_foreign_keys(cat):
    ol = cat._docs["main.sales_order_line"]
    refs = {fk.ref_table for fk in ol.foreign_keys}
    assert {"sales_order", "cat_product"} <= refs

def test_self_referencing_fk_survives(cat):
    assert any(fk.ref_table == "hr_employee"
               for fk in cat._docs["main.hr_employee"].foreign_keys)

def test_view_definition_is_indexed(cat):
    """reorder_point appears only inside the view SQL, not its output columns."""
    txt = cat._docs["main.v_stock_shortfall"].embed_text().lower()
    assert "reorder" in txt
    assert "reorder_point" not in {c.name for c in
                                   cat._docs["main.v_stock_shortfall"].columns}

def test_fk_expansion_adds_unnamed_join_tables(cat):
    sel = cat.select("discount per line", top_k=3)
    assert any(h.reason == "fk" for h in sel.hits)

def test_decoy_is_outranked(cat):
    """billing_invoice is real revenue; sales_invoice_draft is a decoy."""
    sel = cat.select("unpaid invoices over 90 days", top_k=4, expand_fks=False)
    names = [d.name for d in sel.objects]
    assert names[0] == "billing_invoice"

def test_top_k_is_respected_before_expansion(cat):
    sel = cat.select("revenue", top_k=3, expand_fks=False)
    assert len(sel) <= 3

def test_pin_forces_inclusion(cat):
    sel = cat.select("headcount", top_k=3, pin=["core_country"], expand_fks=False)
    assert "core_country" in {d.name for d in sel.objects}
    assert sel.hits[0].reason == "pinned"

def test_hint_overrides_and_unknown_raises(cat):
    cat.hint("billing_payment", "cash receipts")
    assert cat._docs["main.billing_payment"].hint == "cash receipts"
    with pytest.raises(KeyError):
        cat.hint("no_such_table", "x")

def test_object_list_shape_for_select_ai(cat):
    ol = cat.select("revenue by month", top_k=2, expand_fks=False).object_list
    assert all(set(e) <= {"name", "owner"} and "name" in e for e in ol)

def test_prompt_fragment_is_valid_ddl_ish(cat):
    frag = cat.select("stock per warehouse", top_k=3).prompt_fragment()
    assert "inv_stock_level" in frag and "(" in frag and ")" in frag

def test_empty_catalog_returns_empty_selection():
    sel = Catalog().select("anything")
    assert len(sel) == 0 and sel.total_objects == 0

def test_selection_is_deterministic(cat):
    a = cat.select("late shipments by carrier").table_names
    b = cat.select("late shipments by carrier").table_names
    assert a == b

def test_embedder_is_deterministic_across_instances():
    t = ["v_monthly_revenue total net", "hr_department cost centre"]
    assert HashingEmbedder(dim=256).embed(t) == HashingEmbedder(dim=256).embed(t)

def test_embeddings_are_unit_norm():
    v = HashingEmbedder().embed(["billing_invoice total_gross"])[0]
    assert abs(sum(x * x for x in v) - 1.0) < 1e-9

def test_handles_empty_and_unicode_text():
    e = HashingEmbedder()
    assert len(e.embed([""])[0]) == e.dim
    assert len(e.embed(["facturación mensual 月次売上"])[0]) == e.dim
