"""Every worked solution the builder is shown compiles, so it never learns from a wrong one; and the closest are found
for an owner's need."""
import pytest

from app import lang
from app.builder import recipes

from .test_lang import everything


@pytest.mark.parametrize("n", range(len(recipes.RECIPES)))
def test_each_recipe_compiles_on_a_sample_app(n):
    r = recipes.RECIPES[n]
    out = lang.apply(everything(), r["patches"])
    assert not out.problems, (r["need"], out.problems)


def test_the_closest_recipes_are_found_for_an_owners_need():
    found = recipes.for_needs(["Blocks new bookings for clients who owe on an invoice older than 30 days"])
    assert found and "owes" in found[0]["need"]
    found = recipes.for_needs(["No stylist double-booked at overlapping times"])
    assert any("overlapping" in r["need"] for r in found)
    assert recipes.for_needs(["Keeps a list of suppliers"]) == [] or all("supplier" not in r["need"].lower() for r in recipes.for_needs(["Keeps a list of suppliers"]))
