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


def _link(label: str, url: str, top: float, left: float, height: float = 20) -> Element:
    return Element(
        label, "AXLink", ("Main",), actions=("AXPress",), url=url, top=top, left=left, bottom=top + height, in_page=True
    )


def test_page_results_follow_the_grid_the_user_sees():
    from macbrow.ui import page_results

    home = "https://www.youtube.com/"
    snap = Snapshot(
        "Google Chrome",
        "YouTube",
        [
            _link("Shorts", "https://www.youtube.com/shorts", 150, 10),  # navigation, not a video
            _link("", "https://www.youtube.com/watch?v=B", 200, 500),  # thumbnail, second in the row
            _link("Second video by Chan 2M views 1 day ago", "https://www.youtube.com/watch?v=B", 380, 500),
            _link("Third video by Chan 5K views", "https://www.youtube.com/watch?v=C", 600, 10),  # next row
            _link("", "https://www.youtube.com/watch?v=A", 205, 10),  # first in the row, a few px lower
            _link("First video by Chan 1M views 2 years ago", "https://www.youtube.com/watch?v=A", 385, 10),
            _link("Ad", "https://www.googleadservices.com/pagead/aclk?x", 100, 10),
            Element("Menu video", "AXMenuItem", ("menu", "File"), url="", in_menu=True),
        ],
        0.0,
        home,
    )
    assert [e.url[-1] for e in page_results(snap)] == ["A", "B", "C"]
    assert page_results(snap)[0].label.startswith("First video")  # the title link, not the bare thumbnail


def test_page_results_elsewhere_use_title_length():
    from macbrow.ui import page_results

    snap = Snapshot(
        "Safari",
        "Blog",
        [
            _link("Home", "https://blog.example/", 10, 10),
            _link("A long enough article title", "https://blog.example/a", 90, 10),
        ],
        0.0,
        "https://blog.example/",
    )
    assert [e.label for e in page_results(snap)] == ["A long enough article title"]


def test_spoken_label_keeps_only_the_title():
    from macbrow.ui import spoken_label

    assert spoken_label(_link("Lofi beats by Lofi Girl 1.2M views 3 years ago", "", 0, 0)) == "Lofi beats"
    assert spoken_label(_link("Stand by Me", "", 0, 0)) == "Stand by Me"


def test_scrolled_away_frames_are_outside():
    from macbrow.ui import outside

    view = (0, 100, 1000, 800)
    assert outside((10, 1200, 200, 100), view)  # below the fold
    assert not outside((10, 850, 200, 100), view)  # partly visible
    assert not outside((10, 400, 0, 0), view)  # zero-size wrapper: its children may still show


def test_hover_preview_neither_duplicates_nor_reorders():
    # The pointer rests on the middle card: YouTube adds a preview link, higher up and with other
    # URL extras, to the same video. Found live on the home page.
    from macbrow.ui import page_results

    yt = "https://www.youtube.com/watch?v="
    snap = Snapshot(
        "Google Chrome",
        "YouTube",
        [
            _link("Left card title", yt + "L", 819, 277),
            _link("Middle card preview", yt + "M&list=RDM&start_radio=1&pp=x", 592, 600, height=200),
            _link("Middle card title that is longer", yt + "M&pp=y", 819, 676),
            _link("Right card title", yt + "R", 819, 1074),
        ],
        0.0,
        "https://www.youtube.com/",
    )
    assert [e.label for e in page_results(snap)] == [
        "Left card title",
        "Middle card title that is longer",
        "Right card title",
    ]


def test_spoken_label_drops_a_trailing_duration():
    from macbrow.ui import spoken_label

    assert spoken_label(_link("I built an AI supercomputer 34 minutes", "", 0, 0)) == "I built an AI supercomputer"
    assert spoken_label(_link("Mix 1 hour, 6 minutes", "", 0, 0)) == "Mix"


def test_chrome_squeezes_scrolled_away_elements_into_a_sliver():
    # Found live: Chrome reports an element scrolled above the view as a 1 px line on its edge.
    from macbrow.ui import outside

    view = (0, 151, 1440, 689)
    assert outside((730, 151, 618, 1), view)  # scrolled away upwards
    assert outside((730, 839, 618, 1), view)  # ...and downwards
    assert outside((277, 840, 345, 0), view)  # below the view: 0 px high on its bottom edge
    assert not outside((730, 301, 506, 13), view)  # a real small link


def test_pinned_header_hides_what_scrolls_under_it():
    from macbrow.ui import hide_covered

    header_link = _link("YouTube Home", "https://www.youtube.com/", 158, 90)
    under = _link("Card scrolled under the header", "https://www.youtube.com/watch?v=U", 160, 500)
    peeking = _link("Card mostly below it", "https://www.youtube.com/watch?v=P", 190, 500)
    under.bottom, peeking.bottom, header_link.bottom = 200, 260, 190
    kept = hide_covered([header_link, under, peeking], 202, in_header={0})
    assert kept == [header_link, peeking]


def test_youtube_playlist_card_is_one_result():
    from macbrow.ui import page_results

    yt = "https://www.youtube.com/"
    snap = Snapshot(
        "Google Chrome",
        "YouTube",
        [
            # the title is set bigger (23 px) than the songs under it (13 px), and shorter here
            _link("Best 90s Lofi", yt + "watch?v=S1&list=PLabc", 300, 730, height=23),
            _link("Na Milo Kahin Pyar (Slowed and Reverb)", yt + "watch?v=S1&list=PLabc", 330, 730, height=13),
            _link("View full playlist", yt + "playlist?list=PLabc", 400, 730, height=13),
            _link("Ordinary video in a mix", yt + "watch?v=V&list=RDV&start_radio=1", 700, 730),
        ],
        0.0,
        yt + "results?search_query=lofi",
    )
    assert [e.label for e in page_results(snap)] == ["Best 90s Lofi", "Ordinary video in a mix"]


def test_sidebar_links_are_never_results():
    # Found live: YouTube's sidebar "Watch Later" / "Liked videos" are playlists too.
    from macbrow.ui import page_results

    yt = "https://www.youtube.com/"
    watch_later = _link("Watch Later", yt + "playlist?list=WL", 300, 11)
    watch_later.in_page_nav = True
    video = _link("A real search result", yt + "watch?v=V", 310, 730)
    snap = Snapshot("Google Chrome", "YouTube", [watch_later, video], 0.0, yt + "results?search_query=x")
    assert page_results(snap) == [video]


def _page(url: str, *links: Element) -> Snapshot:
    return Snapshot("Google Chrome", "page", list(links), 0.0, url)


def test_heading_links_are_the_results_when_a_page_has_them():
    # Found live on GitHub search: the type filters ("Code (11M) results") and topic tags are
    # links too; the repositories are the links in headings.
    from macbrow.ui import page_results

    gh = "https://github.com/"
    code_tab = _link("Code (11M) results", gh + "search?q=pw&type=code", 300, 16)
    topic = _link("javascript", gh + "topics/javascript", 370, 414)
    repo1 = _link("microsoft/playwright", gh + "microsoft/playwright", 320, 368)
    repo2 = _link("microsoft/playwright-mcp", gh + "microsoft/playwright-mcp", 471, 368)
    for e in (topic, repo1, repo2):
        e.in_main = True
    repo1.heading = repo2.heading = True
    snap = _page(gh + "search?q=pw&type=repositories", code_tab, topic, repo1, repo2)
    assert [e.label for e in page_results(snap)] == ["microsoft/playwright", "microsoft/playwright-mcp"]


def test_wikipedia_skips_its_sister_project_box():
    from macbrow.ui import page_results

    w = "https://en.wikipedia.org/"
    snap = _page(
        w + "w/index.php?search=black+hole",
        _link("Word definitions from Wiktionary", "https://en.wiktionary.org/wiki/Special:Search?x", 548, 831),
        _link("Thumbnail for Black hole", w + "wiki/Black_hole", 564, 44, height=93),
        _link("Black hole", w + "wiki/Black_hole", 567, 148, height=19),
        _link("Help", w + "wiki/Help:Searching", 250, 1119),
        _link("Supermassive black hole", w + "wiki/Supermassive_black_hole", 681, 148, height=19),
    )
    assert [e.label for e in page_results(snap)] == ["Black hole", "Supermassive black hole"]


def test_unknown_site_without_headings_uses_the_main_column():
    from macbrow.ui import page_results

    site = "https://news.example/"
    stories = [_link(f"Story number {i}", f"https://story{i}.example/", 100 + 40 * i, 60) for i in range(4)]
    box = [_link(f"Related {i}", f"{site}related/{i}", 120 + 40 * i, 900) for i in range(2)]
    snap = _page(site, *box, *stories)
    assert [e.label for e in page_results(snap)] == [f"Story number {i}" for i in range(4)]


def test_results_further_down_follow_those_on_screen_and_scrolled_past_ones_never_count():
    from macbrow.ui import page_results

    yt = "https://www.youtube.com/watch?v="
    past = _link("Scrolled past", yt + "P", 151, 730, height=1)
    past.offscreen = -1
    below1 = _link("Below one", yt + "B1", 839, 730, height=1)
    below2 = _link("Below two", yt + "B2", 839, 730, height=1)
    below1.offscreen = below2.offscreen = 1
    on_screen = _link("On screen", yt + "S", 400, 730)
    snap = _page("https://www.youtube.com/results?search_query=x", past, on_screen, below1, below2)
    assert [e.label for e in page_results(snap)] == ["On screen", "Below one", "Below two"]


def test_a_heading_names_the_result_over_a_button_to_the_same_video():
    # Found live in Safari: the channel banner's "Mix" pill links to the same mix as the card below.
    from macbrow.ui import page_results

    yt = "https://www.youtube.com/watch?v=T&list=RDT"
    button = _link("Mix", yt, 337, 1008, height=40)
    title = _link("Mix – Arijit Singh", yt, 590, 288, height=23)
    title.heading = True
    other = _link("Gehra Hua", "https://www.youtube.com/watch?v=G", 590, 661, height=23)
    other.heading = True
    snap = _page("https://www.youtube.com/results?search_query=arijit", button, title, other)
    assert [e.label for e in page_results(snap)] == ["Mix – Arijit Singh", "Gehra Hua"]


def test_a_card_mostly_past_the_side_of_the_view_is_not_seen():
    from macbrow.ui import shows

    view = (0, 151, 1440, 689)
    assert not shows((1407, 600, 345, 23), view)  # carousel's next card: 33 px of 345 inside
    assert shows((1034, 600, 345, 23), view)
    assert shows((10, 300, 12, 12), view)  # a small icon link, all of it showing
    assert not shows((10, 836, 200, 20), view)  # 4 px peeking over the bottom edge


def test_control_names_are_spoken_without_hidden_marks_or_shortcut_hints():
    # Found sending a test mail: Gmail's button is "Send ‪(⌘Enter)‬".
    from macbrow.ui import _clean, spoken_name

    label = _clean("Send ‪(⌘Enter)‬")
    assert label == "Send (⌘Enter)"
    assert spoken_name(label) == "Send"
    assert spoken_name("Bold (Ctrl+B)") == "Bold"
    assert spoken_name("Discard draft (⌘⇧D)") == "Discard draft"
    assert spoken_name("Results (2024)") == "Results (2024)"  # not a shortcut: kept
