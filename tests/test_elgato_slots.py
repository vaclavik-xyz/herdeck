from herdeck.elgato.slots import SlotLeases
from herdeck.model import AgentKey


def k(pane):
    return AgentKey("dev", pane)


def test_initial_assignment_is_reading_order():
    s = SlotLeases()
    s.update([k("p1"), k("p2"), k("p3")])
    assert s.assignment() == {0: k("p1"), 1: k("p2"), 2: k("p3")}


def test_existing_agents_keep_their_ordinal_when_one_vanishes():
    s = SlotLeases()
    s.update([k("p1"), k("p2"), k("p3")])
    s.update([k("p1"), k("p3")])  # p2 gone
    # p1 and p3 must NOT move; ordinal 1 becomes a hole
    assert s.assignment() == {0: k("p1"), 2: k("p3")}


def test_newcomer_fills_lowest_hole_not_the_end():
    s = SlotLeases()
    s.update([k("p1"), k("p2"), k("p3")])
    s.update([k("p1"), k("p3")])  # hole at 1
    s.update([k("p1"), k("p3"), k("p9")])  # p9 is new
    assert s.ordinal_of(k("p9")) == 1
    assert s.ordinal_of(k("p1")) == 0
    assert s.ordinal_of(k("p3")) == 2


def test_overflow_agents_get_offslot_ordinals():
    s = SlotLeases()
    s.update([k(f"p{i}") for i in range(5)])
    assert s.ordinal_of(k("p4")) == 4  # caller decides which ordinals are visible
