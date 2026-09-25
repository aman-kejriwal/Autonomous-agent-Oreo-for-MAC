from macbrow.ui import Element, Snapshot, candidates, choice_criteria, find, pick_confidence, press_args


def _snap(*elements: Element) -> Snapshot:
    return Snapshot("Spotify", "Spotify Premium", list(elements), 0.0)


ROW = Element("Liked Songs Pinned Playlist • pk", "AXRow", ("Main", "Your Library"), actions=("AXPress",))
BUTTON = Element("Liked Songs Pinned Playlist • pk", "AXButton", ("Your Library", "Liked Songs"), actions=("AXPress",))
MENU = Element("New Playlist", "AXMenuItem", ("menu", "File"), in_menu=True)


def test_duplicates_keep_the_element_that_acts():
    # Spotify's rows ignore AXPress; the button inside carries the same label and navigates
    assert candidates(_snap(ROW, BUTTON), "open liked songs") == [BUTTON]
    assert candidates(_snap(BUTTON, ROW), "open liked songs") == [BUTTON]


def test_descriptions_carry_the_place():
    assert BUTTON.describe() == "button 'Liked Songs Pinned Playlist • pk' in Your Library"
    assert MENU.describe() == "menu command 'New Playlist' in the File menu"


def test_candidates_put_matching_words_first_when_capped():
    filler = [Element(f"Item {i}", "AXButton", ("Main",), actions=("AXPress",)) for i in range(20)]
    liked = Element("Liked Songs", "AXButton", ("Your Library",), actions=("AXPress",))
    assert candidates(_snap(*filler, liked), "open the liked songs", limit=5)[0] is liked


def test_same_target_probabilities_add_up():
    probs = {"Arijit Singh": 0.2, "Arijit Singh Artist": 0.15, "Arijit Singh (2)": 0.1, "Home": 0.3, "__none__": 0.25}
    assert round(pick_confidence("Arijit Singh", probs), 2) == 0.45
    assert pick_confidence("Home", probs) == 0.3


def test_choice_keys_are_unique():
    crit, by_key = choice_criteria([BUTTON, Element(BUTTON.label, "AXLink", ("Recents",), actions=("AXPress",))])
    assert list(crit) == [BUTTON.label, f"{BUTTON.label} (2)"]
    assert by_key[BUTTON.label] is BUTTON


def test_dangerous_labels_need_confirmation():
    assert Element("Log Out", "AXMenuItem", ("menu", "Spotify"), in_menu=True).dangerous
    assert Element("Delete playlist", "AXButton", ()).dangerous
    assert not BUTTON.dangerous and not MENU.dangerous


def test_confirmed_press_finds_the_same_element_again():
    snap = _snap(ROW, BUTTON, MENU)
    assert find(snap, press_args(BUTTON)) is BUTTON
    assert find(snap, press_args(MENU)) is MENU


def test_place_skips_the_wrappers_named_after_the_element():
    btn = Element(
        "Liked Songs Pinned Playlist • pk",
        "AXButton",
        ("Main", "Your Library", "Liked Songs Pinned Playlist • pk", "Liked Songs", "Liked Songs"),
        actions=("AXPress",),
    )
    assert btn.where == "Main > Your Library"


def test_search_field_ranking():
    from macbrow.ui import Field

    def f(role="AXTextField", subrole="", label="", in_list=False, in_toolbar=False):
        return Field(ref=None, role=role, subrole=subrole, label=label, in_list=in_list, in_toolbar=in_toolbar)

    notes_search = f(subrole="AXSearchField", in_toolbar=True)
    spotify_box = f(role="AXComboBox", label="What do you want to play?")
    assert notes_search.score > spotify_box.score >= 2
    assert f(label="Search mail").score >= 2
    # never type a search into these
    assert f(label="AI Tools", in_list=True).score == 0  # an editable folder name in Notes' sidebar
    assert f(label="Message #general").score == 0  # Slack's compose box
    assert f(subrole="AXSecureTextField", label="Search").score == 0
    assert f(label="Title").score < 2  # an ordinary text box is not a search box
