# Changelog

> **Version numbers in this file are this fork's own.** They are not upstream
> release numbers and do not line up with them. See "Versioning" in
> `README.md`. Fork releases from 0.6.5 onward carry a `+cfa.N` suffix; the
> earlier `0.6.4` entry predates that convention and **is not** upstream's
> `v0.6.4`, which is different code.

## 1.4.0+cfa.8 - 2026-09-17

A release about the window rather than about the numbers in it. The panel had
been 340 px wide and 420 px tall at most since 1.0, the Settings dialog opened
at a hardcoded size that one tab did not fit in, and the app had no icon at
all. Minor rather than patch: a resizable window and an icon are things the
user sees.

### Added

- **The floating panel resizes, in both dimensions, and remembers it.** Drag
  any edge or corner. An 8 px band on each edge and corner hit-tests to
  `Qt.Edges` - pure arithmetic on a rectangle, so the eight zones are testable
  where there is no window manager to observe - and a press in the band calls
  `QWindow.startSystemResize()`, which is what gets snapping, a live outline
  and a multi-DPI desktop right on Windows, macOS and X11/Wayland alike. Where
  it returns False the geometry is computed from the drag delta instead,
  clamped to the window's own bounds, with the left and top edges moving the
  origin as well as the size. Offscreen it returns False, which is what makes
  the fallback the tested path.

  The floor is **260 x 80**. 260 rather than the 239 the measurement gives: the
  last header control to lose a pixel as the window narrows is the "just now"
  age label, at 239 - the four header buttons are `setFixedSize` and never
  squeeze - and 260 is the first round number above it that also leaves a
  metric row's bar 64 px instead of 44. The ceiling is the **work area of the
  monitor the window is on**, re-applied on show, on `screenChanged` and on
  `availableGeometryChanged`, because that answer is per-monitor and changes
  when a taskbar does. The existing off-screen clamp moved a window that came
  back on a smaller display; it now shrinks one too.

  `WindowState.width` and `.height` are real for the first time. The migration
  that replaced the saved width with the constant 340 on every load and capped
  the height at 420 is gone; both are bounded by the model to
  `[minimum, 4096]` - a sanity bound, since the loader cannot know which
  monitor the window will open on - and the widget clamps to the real screen at
  show time. A 1.3.x config (340 x 420) loads unchanged.

  **Auto-fit did not go away; it became conditional, and the docstring says
  which rule applies when.** Collapsed is always auto-fitted: a chip strip has
  one right height and the user cannot drag it into two rows. Expanded is
  auto-fitted only while the window has never been sized by hand. After that
  the saved size wins and the tile area takes the difference - re-fitting on
  the next refresh is exactly what would undo the drag.

  "Sized by hand" is a **recorded** answer, `WindowState.user_sized`, not one
  inferred from the numbers. Inferring it as "the size is not exactly
  340 x 220" would have switched auto-fit off for every existing user on their
  first launch: 1.3.x wrote its *auto-fitted* height back on every release,
  hide and close, so an upgrading config almost never carries 220. A 1.3.x
  file has no such key, which is the right answer - False - and its width is
  still restored. It is set by a resize that **changed the size**: in the
  fallback path on the first delta that moves an edge, and on the native path
  on the first `resizeEvent` the window manager's drag produces that changes
  the size. It used to be set on the *press*, so one motionless click 1 px
  inside the 8 px band ended auto-fit for good and the release wrote that size
  to disk. Like the other bounded window fields it is coerced rather than
  trusted: anything that is not a real bool reads as False.

  **Which resize is the user's is recorded, not inferred.** Qt delivers the
  move or resize the app asks for itself exactly as it delivers the window
  manager's, so auto-fit, the collapse/expand height restore, the screen
  clamp, the work-area cap and the macOS popover anchor all run behind a depth
  counter, and an event that arrives with it above zero chooses no size and
  arms no write. Without it a motionless click in the band was still fatal on
  the native path: `startSystemResize` returns True there, the window manager
  keeps the release, and a gesture the user ends without moving produces no
  event at all - so the flags the press set stayed set for the life of the
  window and the next auto-fit growth was written back as the size the user
  chose. The press now starts the same one-second debounce, which is what ends
  such a gesture; offscreen `startSystemResize` returns False, so the test for
  it patches the `QWindow` to return True the way every real desktop does.

  **The geometry is written back from one seam, `_commit_geometry()`**, and
  everything that can end a gesture reaches it: the mouse release, a hide (the
  ✕ button hides rather than closes, and `App.shutdown()` hides and quits), a
  close, and - for the drags that end in the window manager - a one-second
  single-shot debounce armed by `moveEvent`/`resizeEvent` while
  `startSystemMove`/`startSystemResize` owns the pointer. That last one is the
  path every real desktop takes and the one offscreen cannot see: the WM keeps
  the release, so before this the position and the size of a WM-ended drag
  reached the file only if the user later closed the window. The seam writes
  x, y, width, height and collapsed together and saves only when one of them
  changed, so a drag is one atomic write however many events it took, a
  hide/show cycle that moved nothing writes nothing, and the clamp onto a
  visible screen is re-run when the debounce fires with no mouse button down.

  **The debounce is a commit, not the end of the gesture.** A pointer held
  still for a second inside a drag produces exactly the silence that ends one,
  and nothing in the event stream tells them apart. So the timer writes and
  lets the next event re-arm - arming is on any geometry change the app did
  not make itself, whatever the flags say - and a drag with a pause in it is
  two writes rather than one truncated at the pause. The clamp is the part
  that cannot be guessed at: it moves and resizes the window, and doing that
  while the window manager still owns the pointer is the app fighting a live
  drag, measured at a 240 px jump out from under it. It runs only when Qt
  reports no button down, and if that state is stale it is skipped - the next
  show or screen change clamps anyway, which is the safe direction.

  The collapse toggle goes through the same seam. It was the one write path
  outside the dirty check - an unconditional `save()` that left the seam's
  record of the file untouched, so a collapse and the hide after it wrote the
  same state twice - and its `_apply_collapsed_state(save=True)` branch had no
  caller at all. And the commit does **not** persist a position the app chose
  for itself: `show_as_popover` anchors the panel under the macOS menu-bar
  item on every open, and with the dismissal's hide now reaching the seam,
  that x/y would have replaced the position the user dragged to. The size and
  the collapsed state are still written; the first move the user makes hands
  the position back.

- **An app icon.** Three stacked pill bars at 47 %, 72 % and 92 % on the app's
  own rounded dark panel - the compact chip row the widget already shows, which
  is what survives 16 px where a dial's needle becomes a smudge. The band
  colours are read from `ColorThresholds`, so the icon and the tiles cannot
  disagree about what green means, and every measurement in the drawing is a
  fraction of the icon's own size, so the 16 px entry is the 1024 px entry
  scaled rather than a second drawing.

  `tools/make_icon.py` draws it with QPainter and writes both containers
  itself - stdlib plus PyQt6, no Pillow and no icon toolchain, because a
  dependency added to draw one picture is a dependency in the shipped binary's
  supply chain for the life of the project. **ICO**: 14 335 bytes, a 6-byte
  header, one 16-byte directory entry per size and a PNG blob each, at
  16/24/32/48/64/128/256 - with 256 written as 0 in the one-byte width field,
  because that is what the format does with it. **ICNS**: 80 486 bytes, 11
  chunks - `icp4`/`icp5`/`icp6` at 16/32/64, `ic07`-`ic10` at
  128/256/512/1024, and the four @2x types `ic11`-`ic14` at 32/64/256/512,
  since the container has no scale field and a Retina entry is simply a
  different OSType holding a bigger PNG. Both parse back in the tests.

  The generated files are committed beside the script and a test regenerates
  all of them into a temp directory and compares the **decoded pixels**,
  within 8/255 per channel, plus the container structure exactly - entry
  lists, declared lengths, offsets and chunk CRCs. Comparing the PNG streams
  was the first cut and it failed on all three runners: Qt's encoder does not
  produce the same bytes on every build for the same picture, though the
  picture itself is deterministic. A moved edge or a changed colour lands at
  255, so the tolerance buys nothing a drift could hide. The script also sets
  `sys.dont_write_bytecode` before it imports `aigauge.config` for the band
  colours, so a run leaves the four assets and nothing else. `build.ps1` passes the
  `.ico`, `build.sh` the `.icns` on macOS and the PNG elsewhere, and a runtime
  copy at `src/aigauge/assets/ai-gauge-256.png` travels inside the package the
  way the meter catalog does, so one package-relative lookup answers in a
  source checkout, in a wheel and in a frozen bundle alike.
  `QApplication.setWindowIcon` at startup gives it to every top-level window
  at once. It does not undo the Dock trade-off `build.sh` documents:
  `LSUIElement` is what hides the Dock icon for the menu-bar build, and a
  bundle icon is what Finder draws on the `.app`, which a menu-bar agent still
  has. The tray keeps its status dot and its menu-bar pixmap - that icon
  carries the worst band's colour and an app icon cannot.

### Changed

- **Every Settings tab scrolls, and the dialog opens at the height its landing
  page needs.** The Microsoft tab wants 1 765 px. The pane gave it 454 at the
  hardcoded 620 x 520 and there was no scroll bar anywhere, so the bottom of
  the Copilot block was unreachable. The over-tall-window symptom other
  platforms show is the same defect from the other side: the only thing holding
  the dialog down was `setMinimumSize(560, 420)`. Remove that one line and the
  layout's own floor takes over - measured, `minimumSize()` becomes 519 x 1112
  and a `resize(620, 300)` clamps to 620 x 1112 - because the Microsoft page's
  `minimumSizeHint` is 1 015 px on its own, and every Azure row added pushes
  both hints up.

  Every tab is now added through one helper that wraps its page in a
  `QScrollArea`, and there is no longer a code path that adds a bare page, so a
  future tab gets the behaviour without anyone remembering to. A scroll area's
  size hint is its widget's bounded to 36 x 24 font heights (504 x 336 here),
  which is what breaks the link between a page's content and the window's
  floor: the dialog's `minimumSizeHint` goes **519 x 1112 → 271 x 155** and the
  tab widget's **495 x 1046 → 127 x 89**.

  The default size is a measurement rather than a number. General is the
  landing page, so the dialog opens at the height General lays out at plus the
  chrome around it, both read off the live widgets. The chrome is 12 + 10
  margins, 10 spacing, a 34 px button row, a 27 px tab bar and 4 px of pane,
  i.e. 97, plus 6 px of declared slack, because a style rounds its tab pane
  differently in a hint than in a layout (Fusion's laid-out pane is 1 px, so
  the number is 3 px generous here and could be 3 px short elsewhere). The
  page's need is its height-for-width at the width the scroll area decides
  with - the viewport less a scroll bar, 582 px, since a `QScrollArea`'s first
  pass runs with the bar reserved and a page that needs it at that width keeps
  it - never less than its minimum hint, and not its `sizeHint`, which for a
  page with a word-wrapped label is the height at 80 average characters, not at
  the width it gets: General is 486 px at 582 (minimum 483, `sizeHint` 498).
  On the macOS runner the same label takes one more line at 584 than at 594
  (505 px against 493), which is what measuring at the wider width missed. A
  page whose own minimum is wider than the viewport is laid out at that
  minimum and clipped, so it is measured there - and the dialog's width, 620
  by default, grows to the widest page's minimum plus the same chrome, up to
  the work area - which is how the Windows runner's fonts, a third wider than
  Linux's (General 612 px against 454), get a dialog as wide as its pages need
  rather than one that clips them. **The floor the user may drag to is the
  same rule**, not the bare 560: at 560 on those fonts a page lost its
  right-hand 46-80 px with the range sitting in a bar the policy hid. And the
  pages' horizontal bars are `ScrollBarAsNeeded` like their vertical ones -
  `AlwaysOff` never made a page fit, it only hid the evidence - so on a work
  area narrower than a page, where the floor has to give way to the screen,
  the rest of the page is still reachable.
  And it is read only after every layout under the dialog has been activated,
  deepest first. A widget's `updateGeometry()` reaches only its parent's
  top-level layout, a hidden widget drops the `LayoutRequest`, and the nested
  row holding the UI-scale combo box was still serving the 22 px it had cached
  before the combo was styled to 32. That is how the first cut under-read
  General by 10 px and opened it with a scroll bar on the Windows and macOS
  runners (7 and 6 px of range) while offscreen Linux passed on 5 px of font
  luck. The dialog opens **620 x 589** against the hardcoded 520; a binary
  search for the smallest height at which General shows no scroll bar gives
  580, so 9 px of that is slack, and a test now holds the default within the
  slack of that search from both sides. Then `showEvent` measures once more,
  with the real viewport: the tree is polished and shown by then, so after the
  layouts are activated every geometry is the laid-out one, and a deficit
  grows the dialog before its first paint. That is the guarantee behind the
  estimate on a platform whose fonts or style land away from their hints;
  offscreen it is a measurement and no resize, and the test that pins the
  default warns with both sets of terms wherever it is not. The chrome is
  deliberately not derived as `height() - viewport().height()` before `show()`: the viewport is
  still at its unlaid 640 x 480 then and the subtraction comes out **-60**,
  which yields a 428 px dialog in which General itself scrolls. Both ends are
  clamped to the screen's work area, the minimum included - at 200 % display
  scale every logical hint is unchanged while `availableGeometry()` shrinks
  800 → 400, so an un-shrinkable 560 x 420 minimum is a dialog whose OK button
  cannot be reached. **The ceiling out-ranks the floor**, in the clamp and in
  the minimum alike: `max(floor, min(content, ceiling))` let a 420 floor beat a
  360 ceiling, and the minimum was clamped to the raw work area rather than to
  the ceiling, so at 200 % the dialog was sized 400 x 420 - 20 px taller than
  the whole desktop - before any paint, and the `showEvent` re-fit was doing
  the estimate's job. Measured again at `QT_SCALE_FACTOR=2`: 400 x 360 before
  the show and 400 x 360 after it. Nothing persists the dialog's size, which was already true
  and is now pinned by a test.

- **One scroll-bar stylesheet, and it is visible.** The widget's tile area drew
  a 6 px handle on a track the same colour as the panel - a floating sliver
  with no trough - and the dialog drew whatever Qt's default style felt like.
  Both now come from `ui_style`: 10 px, a track one step up the grey ramp from
  whichever panel it is on, a `#4b5563` handle with 2 px margins that lightens
  to `#6b7280` on hover and `#9ca3af` when pressed, no arrow buttons, a 24 px
  minimum handle. Rendered and sampled at 12 tiles: handle `#4b5563`, track
  `#1f2937`, panel `#111827` beside it, and `#111827` in that column on a
  window whose content fits. In the dialog: viewport `#1f2937`, track
  `#374151`, handle `#4b5563`, and nothing painted for a tab that fits. A
  `QScrollArea`'s viewport has `autoFillBackground()` True and a Window
  background role, and neither `setAutoFillBackground(False)` nor a rule on
  `QScrollArea` itself reaches it - measured `#efefef` through the dark dialog
  in both cases; only the descendant rule does, which is the same idiom
  `widget.py` already used and the same trap this changelog records once
  before.

  The widget's tile area now shows a **horizontal** bar as well, and it is not
  theoretical: a plain metric row's minimum width is 196 px, but an Azure row
  carrying a spend and an allowance in its reset column measures 304, so at the
  260 px window minimum that content really is wider than the viewport. A wheel
  notch is three lines of text (42 px at this font) against Qt's default 20,
  and a page step is the viewport. The step is `ui_style.WHEEL_STEP_LINES` and
  the Settings pages take it too - they had kept Qt's 20, so the same gesture
  moved two different distances in the same app. A count of lines and not a
  pixel number, because the two surfaces have different fonts and the three
  platforms' differ by up to a third.

### Fixed

- **A press on the panel no longer raises Settings, so dragging works.** The
  user reported that the panel could not be dragged once the Azure tile's rows
  appeared. The hypothesis that a child accepts the press once the tile area
  scrolls is **disproved**: sending a press to the deepest child under each
  point and asking whether it consumed it, nothing in the panel accepts one
  except the four header buttons and a tile's chevron, with two rows and with a
  540 px tile stack that scrolls alike. What is real is the other one. Every
  press emitted `activated_requested`, and the App answers that by calling
  `show()`, `raise_()` and `activateWindow()` on the Settings dialog -
  activating another top-level window while a button is down takes the focus,
  and with it the implicit mouse grab, away from the panel, so the drag dies on
  the first move. That emit now happens on a **release that did not move the
  window**, which is a click. The move itself is handed to the window manager
  through `startSystemMove()`, which removes the whole class - started on the
  first movement past `startDragDistance()` rather than on the press, because
  the WM takes the pointer when it starts and the release never arrives, so
  starting it on the press would have traded the drag defect for a click that
  no longer raises Settings.

- **The Azure tile says when it will actually try again.** On a 429 it read the
  server's `Retry-After` and said "retrying in N min", and then did no such
  thing: what governs the next attempt is `next_allowed_at`, the later of the
  hourly floor from `last_fetch_at` and `blocked_until`. Measured on the user's
  desktop, a 52 s `Retry-After` at 11:16:48 was followed by every refresh until
  12:16 being served from the cached error in 0.0 s, under a message promising
  one minute. It now reads "Cost Management is rate limiting this tenant; next
  attempt at HH:MM.", in local time and with the same `%H:%M` as
  `_stale_settings_note` - the same promise about the same clock, on the same
  tile. A `Retry-After` longer than the hour is still what is named, because
  `blocked_until` is then the later of the two, and `next_allowed_at` is
  floored at the clock, so neither sentence can name a time that has already
  gone by - `max(candidates)` on a state with no `last_fetch_at` and an
  expired `blocked_until` said "next attempt at 14:09" at 17:09.

- **An error tile with no rows says what happened.** Reproduced offscreen with
  an empty metric list and a 68-character error: the status line was there, as
  26 px of the word "error" in the far right corner of a 340 px header, on a
  tile 22 px tall, with the message reachable only by hovering it. So the
  report - "the title and nothing else" - is accurate about what the tile
  communicates and wrong only about the mechanism. Two changes. The corner tag
  names the failure: `_short_error_reason` matched timeout, load failure,
  layout change, no data, api and signed out and nothing else, so Azure's 429 -
  the most common error this app shows - fell through to a bare "error"; a rate
  limit, a throttle or a bare `429` now reads "error · rate limited", 92 px
  against 26, and that branch is tested **before** the api one, because a
  string saying both ("GitHub API rate limit exceeded") is a throttle first. And a
  tile with no rows at all gets the message itself on a full-width line under
  the header - 324 px, and the tile grows from 22 px to 36 - elided to the
  window's current width with the full text in the tooltip, and clickable to
  the same details dialog - on the **release**, and only if the pointer stayed
  within `startDragDistance()`, which is the same rule the panel applies to
  its own press. The line is `Qt.TextFormat.PlainText`, explicitly: the
  default is `AutoText`, so an error string shaped like markup was
  *interpreted* - measured, a 67 px hint against the 287 px the same string
  costs as plain text, which is a provider string choosing what an error tile
  says and in what colour, and a 38 kB `<table>` laid the label out as a
  table. Both tooltips carrying an error - the line's and the status label's -
  are clipped to 280 characters and HTML-escaped, since `QToolTip` has no
  text-format setter and a 10 kB error made a 10 kB popup. Escaped **and
  wrapped**: `Qt::mightBeRichText` reads only the *first line* for a `<` or a
  literal `&lt;`, so an escaped string with no markup up there was drawn as
  plain text and the escapes themselves were shown - `R&D` came out `R&amp;D`,
  and markup on line two came out as entities. A
  `<div style='white-space:pre-wrap'>` takes the decision away from the
  heuristic and keeps the blank line HTML would collapse, while the clip stays
  on the raw string where it cannot cut an entity in half - which bounds the
  tooltip at 281 x 6 characters plus the suffix and the wrapper, 1 181 for a
  10 kB error made entirely of `<`. The line itself is flattened to one line
  and clipped to 1 000 characters before it is elided: `setWordWrap(False)`
  does not stop an explicit newline, so a 5 000-line error laid the label out
  660 px tall, and `elidedText` measures what it is handed - a 1 MB error cost
  284 ms on the UI thread. Emitting on the press would have put a new top-level window
  under a button that is still down, i.e. the defect below, over 324 px of a
  340 px panel. Only when there is nothing else on the tile: with
  rows present the tag reads "error · stale" beside numbers that explain
  themselves. The same line covers `AUTH_REQUIRED` for the three providers with
  no Sign in button, where "not signed in" in the corner was the whole message.

- **An Azure component row's amount is on every part of the row.** A Windows 11
  desktop showed no tooltip when hovering an "Azure App Service" row, although
  the row carries the metric's note. It could not be reproduced: a ToolTip help
  event sent to the row, its label, its bar, its percentage, the inner
  `QProgressBar` and the pace overlay each produced the amount, and nothing on
  the hover path could be made to hide an open tooltip - window opacity at 1.0
  and 0.8, the enterEvent path, a resize, `_do_refit_height`, the one-second
  header tick, the collapsed-summary rebuild, a refresh dim, `raise_()`,
  re-applying the always-on-top flag and re-delivering the snapshot all leave
  it standing. The offscreen plugin has no real window activation or native
  stacking order, so that is evidence the cause is not in the app's own event
  flow rather than proof about Windows. Rather than guess, the fix removes the
  assumption the propagation rests on - that every child under the pointer
  answers a ToolTip event with nothing - by writing the note onto the row's
  label and percentage as well, on every `set_metric`, so a row reused for a
  metric with no note cannot keep the previous one's.

### Notes

- The suite is **1 981 tests**, from 1 859. `tests/test_icon.py` is new and
  holds 13. `tests/test_widget.py` goes 57 → 124,
  `tests/test_settings_dialog.py` 33 → 52, `tests/test_config.py` 138 → 154
  and `tests/test_azure.py` 232 → 239. Counted at the head of the branch, not
  at the first draft of it: the figures this entry carried before were the
  ones from before the CI fixes and the review round below.

- **A review round changed seven behaviours and closed six test gaps.** Two
  defects in the headline feature: collapsing and expanding wrote the 58 px
  chip strip's height over the size the user had dragged to, and the geometry
  was saved only from a mouse release the window manager never delivers. Two
  in what turns auto-fit off: a motionless click inside the 8 px band, and an
  inferred `user_sized` that read True for every 1.3.x config. One re-created
  the focus steal this release exists to fix, on the new error line. One left
  a Settings page clipped at the dialog's floor with the bar policy hiding the
  range. One let the height floor out-rank the screen ceiling. Each is
  described in the section it belongs to above. The six gaps were mutations
  the suite walked through: the 8 px band's own width, the left/top branches
  of the fallback resize, the `_collapsed` guard on the size write, the two
  redundant screen clamps, and the deepest-first layout pass.

- **Two tests had to change**, both because they pinned the behaviour this
  release replaces. `test_widget_uses_fixed_width_despite_extreme_saved_size`
  asserted that a 5 000 px saved width became 340; the answer is now "fit it to
  the monitor", and the replacement asserts that. `test_refit_restores_fixed_width_after_dpi_resize_glitch`
  asserted that the re-fit restores 340 after a DPI glitch; that is now
  precisely the thing that would undo a drag, and the replacement asserts the
  re-fit leaves a chosen width alone. Both replacements name what they
  replace.

- **`WINDOW_MAX_HEIGHT` is now `WINDOW_AUTOFIT_MAX_HEIGHT`.** The value is
  unchanged at 420 and so is its job - the ceiling on a height the *app* picks,
  so a fresh install with six providers does not open most of a screen tall -
  but it is no longer a ceiling on the window, and a constant whose name says
  otherwise is how the next change gets it wrong. `WINDOW_MIN_WIDTH` (260),
  `WINDOW_DEFAULT_HEIGHT` (220) and `WINDOW_MAX_DIMENSION` (4096) are new.
  `docs/ui-scale-widget-only-plan.md` still names the old constant; it is a
  proposal that has not been started and was left alone.

- Measured offscreen throughout: one 800 x 800 screen, a 14 px font, and
  `QWindow.startSystemResize()` / `startSystemMove()` both returning False,
  which is exactly the platform the pure-Qt fallback exists for.

## 1.3.2+cfa.7 - 2026-09-16

The residuals of the hardening follow-up: the socket itself, the write that
holds every setting, and two things the documents did not say. No behaviour
changes for a healthy provider - the same requests to the same hosts, and a
call that answers in under a second answers exactly as it did.

### Changed

- **A REST request is bounded end to end, not per socket read.** `requests`'
  `timeout=` bounds one connect or one read, never the exchange, so a server
  sending a byte every 14 s against a 15 s timeout is a healthy connection as
  far as `requests` is concerned - and it held the `QThreadPool` worker
  reading it for as long as it cared to keep dripping. 1.3.1+cfa.6 bounded
  how many such workers could pile up and what one cost the log; nothing
  bounded how long one lived, and the pool is global, so a stuck worker was a
  stuck slot until the process exited. One helper,
  `providers/_http.py`, now bounds the exchange in both halves it has. In
  band: a clock started before the call, `stream=True`, and the body drained
  here, with the elapsed time and the running byte count checked between
  reads. Out of band: a `threading.Timer` armed with the same deadline, which
  shuts that connection's socket down from another thread. Copilot's five
  call sites, OpenRouter's three, Azure's two and the Entra ID token POST all
  go through it. Every failure here subclasses `requests.RequestException`, so
  the handlers already at those sites take them, and none of them retries.
  (One site did not have such a handler at all - see Copilot's, below.)

  **The timer is what makes it a bound**, and it is not belt and braces. A
  check between reads bounds nothing that happens *inside* one read, and two
  things do: the response headers (`http.client` reads them with repeated
  `readline()`s, and each arriving byte resets the per-socket timeout) and a
  `Content-Encoding` body whose bytes decode to nothing (urllib3's `read1`
  loops internally until the decoder yields, and an empty DEFLATE stored
  block is five bytes in and zero bytes out). `requests` asks for
  `gzip, deflate` on every call, so both are reachable everywhere. urllib3's
  own `Timeout(total=…)` does not help - it clamps the value of the per-read
  timeout, which every byte resets; measured, a 30 s header drip returned at
  30.01 s against a 3 s total. Shutting the socket down is the one thing that
  reaches a thread blocked in `recv`, on POSIX and on Windows alike. The
  connection to shut down is learned from a small `HTTPAdapter` subclass
  mounted on a `Session` built for that one call and discarded with it - so
  no pool, cookie jar or connection survives a refresh, exactly as
  `requests.request` already behaved.

  **A timer that fires before the socket exists looks again**, every 0.25 s
  until the call ends. `getaddrinfo` runs before any socket is made and
  outside every timeout this app sets, so a resolver slower than the deadline
  used to leave the timer with nothing to shut down - and, because it fired
  only once, with nothing bounding the status line, the header block or the
  body after it either. Measured at the scaled constants, that was 40 s and
  70 s against a 4.0 s bound, both ended by the harness rather than by the
  app; with the re-arm it is 3.75 s. The same look-again covers a socket the
  timer can see but cannot reach: for the whole of a TLS handshake urllib3
  still holds the plain socket, which `wrap_socket` has already detached, so
  the shutdown raises EBADF - and swallowing that gave the deadline away for
  the rest of the call on the path all four hosts use. Measured over real
  TLS against a 12.0 s bound, a deadline landing inside the handshake cost
  **23.0 s** and a re-arm landing there **22.5 s**, and one dripped header
  line was unbounded - still inside the call when the harness gave up; with
  that branch looking again too they are 3.25 s and 2.56 s. Name resolution
  itself is still outside the bound, here as in plain `requests`, and the
  docstring, `SECURITY.md` and `docs/next-session.md` 8.3 now say so instead
  of implying otherwise: one call returns or raises within 45 s **of the
  socket**, plus whatever the resolver spends before it.

  **A deadline that cannot be armed refuses the call.** `threading.Timer`
  needs a thread, and `Thread.start()` raises `RuntimeError` when the process
  has none to give. On the first arm that walked out of the helper past every
  `except requests.RequestException` branch its callers have; it now raises
  `DeadlineUnavailable`, a `RequestException` with a fixed message, before
  the request is made. A re-arm that cannot start used to die on the timer's
  own thread - a traceback to stderr, which the packaged build discards -
  leaving the exchange bounded by nothing and the log empty; it now writes
  one `provider http deadline_rearm_failed=True` and marks the deadline, so
  the next in-band check ends the call. That is not the bound restored: a
  call already stalled in a header read never reaches an in-band check, so it
  stays unbounded in that state - measured, still inside the call at a 14 s
  give-up against a 4.0 s bound. What changed is that it is no longer silent.

  Measured on a dripping loopback server with the constants scaled down
  (1 s socket timeout, 3 s total, so the promised bound is 4.0 s). Before, on
  the body: a 40-byte drip - 8 s of server - returned after **7.81 s**, the
  1 s timeout never firing once; a 5 000-byte drip **still held the worker
  when the harness gave up watching at 30.0 s**, and would have held it for
  1 000 s. Before, in the phases a between-reads check cannot see: a dripped
  status line, a dripped header block, a header block that never ends, and a
  chunked or `Content-Length` gzip stream that decodes to nothing all **held
  the worker to the 40 s cap the harness watched to**, with the
  `iter_content(1)` fallback no better. After: **`ResponseDeadlineExceeded`
  at 3.00 s on every one of them**, and on the body drips, inside the
  predicted 4.0 s. In the shipped units (30 s total, 15 s timeout, declared
  worst case 45 s) a header drip of one byte every 10 s went from
  **unbounded** - still inside the call when a 100 s harness gave up watching
  - to **30.0 s**. (An earlier draft of this entry said 90.0 s there. That is
  the second at which the harness's server stopped dripping, not the one at
  which the call ended.) A server flooding chunked data against a 1 MiB cap:
  `ResponseTooLarge` at 0.00 s. (An earlier draft of this entry said how many
  bytes that server had managed to push; that number says how fast the server
  got going, not anything about the bound, and it did not reproduce.)

  The obvious implementation does not work, which is the part worth knowing.
  `iter_content(chunk_size=N)` goes through urllib3's `stream()`, which
  blocks "until `amt` bytes have been read from the connection or until the
  connection is closed" - and the per-socket timeout never fires on a server
  dripping inside it. Against a loopback server at one byte per 50 ms behind
  a 2 s socket timeout, `iter_content(64 KiB)` **never yielded at all**,
  `iter_content(1)` yielded at once, and `raw.read1()` yielded at once; on a
  1 MiB body delivered in one go the three cost 0.8 ms, **3 854 ms** and
  0.5 ms. So the drain is `read1`, with `iter_content(1)` behind it for a
  handle that has none.

- **The three REST providers now tell the App a budget that is true.** One
  call returns or raises by `max(connect, total) + read`, which at a 30 s
  total is 45 s for a 15 s timeout and 40 s for a 10 s one. Azure's
  `REFRESH_WORST_CASE_SECONDS` was 90 + 9 x 15 = **225 s** with the
  per-request term a socket timeout; it is 90 + 9 x 45 = **495 s** with the
  per-request term a whole call. The number grew because it is now true.
  Copilot's three sequential calls are 40 + 45 + 45 = **130 s** and
  OpenRouter's three are 3 x 45 = **135 s**, neither of which fits the flat
  60 s a plain REST provider used to take, so both now declare
  `refresh_budget_seconds` the way Azure does - which also closes the
  "neither declares one" note in `docs/next-session.md` 8.3. A longer
  watchdog is the right direction: the watchdog ends the App's wait, and
  until now it was the only thing that ended a wedged REST refresh at all. A
  provider that gives up on its own at 130, 135 or 495 s closes its own cycle,
  so the ceiling is reached less often than before rather than more.

- **The 8 MiB memory ceiling now has the floor it was standing on, and a
  refusal that does not need one.** The cap counts *decoded* bytes, and that
  only bounds memory if one read cannot produce a gigabyte by itself. Every
  urllib3 2.x returns at most `chunk_bytes` of decoded bytes from `read1`, but
  only 2.6 and later stop the decoder at `max_length`: on 2.4 and 2.5 the whole
  raw read is decoded first and the surplus buffered. `requests>=2.32` asks
  only for `urllib3>=1.21.1,<3`, and neither `build.sh` nor `build.ps1` pins
  one, so the comment promising that "a hostile or broken endpoint cannot make
  a background worker allocate a gigabyte" was true of the resolved version
  rather than of this code. Measured on 2.5.0, a **988-byte** response with
  `Content-Encoding: gzip, gzip` cost **1 070 MiB** and the whole 30 s
  deadline (9.8 MiB on the shipped 2.6.3).

  Both halves are taken. `pyproject.toml` declares `urllib3>=2.6` - requests'
  own transitive dependency made explicit, not a new one - and a response
  whose `Content-Encoding` header contains a **comma** is refused before a
  byte of its body is read, with `ResponseEncodingRefused`, another
  `requests.RequestException`. No host this app speaks to serves nested
  codings. After: **0.2 MiB and 0.01 s** on 2.6.3 *and* on 2.5.0, with the
  body never read. A single-layer gzip bomb is unchanged - `ResponseTooLarge`
  at the cap, with a `tracemalloc` peak the suite now pins under 32 MiB.

  The comma, rather than a count of the codings named, because the comma is
  what urllib3 decides on: `_init_decoder` sends any header containing one to
  `MultiDecoder`, which splits without dropping empty entries and gives every
  name it does not recognise - `""` included - a `DeflateDecoder`. A first cut
  of this refusal counted the non-empty names, which made
  `Content-Encoding: gzip,` one coding here and two decoder layers there:
  measured, a `deflate(gzip(16 MiB of zeros))` body under that header walked
  through the refusal and had both layers decoded (8.8 MiB of peak on the
  shipped 2.6.3, and on 2.5.0 it is the 1 070 MiB shape the refusal exists to
  stop). `identity, gzip` and `gzip, identity` are refused too, though each
  names one real coding: urllib3 builds the same two-layer decoder for them,
  whose `identity` layer is a `DeflateDecoder` that fails on the plain
  output.

- **`allow_redirects=False` on every REST call, and a 3xx is reported as
  one.** Every host this app speaks to is fixed and listed in `SECURITY.md`,
  so a redirect is a failure, not something to follow. Azure already said so;
  Copilot and OpenRouter get the helper's default. Refusing the hop is only
  half of it, though: `raise_for_status()` says nothing about a 3xx, so the
  redirect went on to `.json()` and the tile read *"Expecting value: line 1
  column 1 (char 0)"* - and on Copilot's username resolve it became a `None`,
  which the tile reports as *"PAT may lack read:user"*, sending the user to
  re-issue a credential that is fine. A 3xx now raises `ResponseRedirected`,
  another `requests.RequestException`, carrying the status and neither the
  URL nor the `Location`. `_resolve_username` lets that one through rather
  than swallowing it, because "the PAT may lack read:user" is the wrong
  diagnosis for a redirect.

  Whether these paths ever redirect is an assumption, not a measurement: it
  was not tested against the live hosts, and `api.github.com` is documented
  to answer a renamed user or organisation with a 301. If one does, the tile
  now says so instead of guessing.

  Only the statuses that carry a `Location` are called a redirect -
  `{301, 302, 303, 307, 308}`. The rest of the 3xx range still fails closed,
  because this app reads a 2xx and nothing else, but says "The endpoint
  returned 304; this app reads only a 2xx." A `304 Not Modified` is not a
  redirect, and it is the one status in that range a caller here could
  actually meet: no call site sends `If-None-Match` today, but that is the
  natural way to spend fewer of GitHub's rate-limit units, and a caching
  proxy can send one unasked.

### Fixed

- **Going offline no longer puts the GitHub username in the log, on the tile
  and in Copy diagnostics.** Copilot's `work()` caught `requests.HTTPError` -
  a reply GitHub actually sent - at each of its three branches, and nothing
  caught the rest, so an unresolvable host or a dropped connection fell
  through to the worker's blanket handler, which did `log.exception` (a
  traceback whose last line is the exception message) and reported
  `str(exc)`. A `requests` connection error carries the URL it failed on, and
  a Copilot usage URL carries the username as a path segment, so the account
  identifier reached all three sinks SECURITY.md says it never reaches - twice
  in the log. Not a regression: byte-identical on 1.3.1+cfa.6. The PAT was
  never in any of them.

  `work()` now has its own `except requests.RequestException` branch -
  `GitHub request failed (TypeName).`, with a
  `classification=request_failed type=…` log line and no traceback - and it
  wraps the username resolve as well as the two fetches. The blanket handler
  behind it reports the type name too, the way azure's already did. This is
  also what makes the transport helper's own claim true: until now
  `_http.py` said every call site had a `RequestException` branch, and one
  did not.

  The type name is the rule for a `requests` exception, whose message *is*
  the URL it failed on. The five the bounded helper raises are built out of a
  status, a count or a bound and carry no URL by construction, so those reach
  the tile as themselves - `GitHub request failed: The endpoint redirected
  (301); this app does not follow redirects.` - as OpenRouter's and Azure's
  already do.

- **A truncated reply no longer reads as a bad PAT.** A server or a middlebox
  that drops the connection inside the header block produces an ordinary 200
  with no body: `http.client` treats EOF as the end of the headers, so the
  call gets `status_code=200`, `content=b''` (plain `requests` does the same
  - not this release's doing). `.json()` on that raises
  `requests.exceptions.JSONDecodeError`, which is a `RequestException`, so
  `_resolve_username` turned it into `None` and the tile into *"Could not
  resolve GitHub username (PAT may lack read:user)"* - the same wrong
  diagnosis the redirect fix above removed, reached by a different route.
  `InvalidJSONError` now goes to `work()`'s branch instead:
  `GitHub request failed (JSONDecodeError).`

  That was the second route closed one at a time, and the rule behind them
  was what was wrong: `_resolve_username` answered `None` - "PAT may lack
  read:user" - for **every** transport failure it did not name, which still
  covered four of the five exceptions this release's own helper raises, a
  500, and an ordinary offline machine. The rule is now the one the message
  is a diagnosis of: GitHub answered and refused, which is a 401 or a 403 on
  `/user`. Measured through the tile, the deadline, an oversized reply, a
  refused encoding, a `ConnectionError`, a `ReadTimeout` and a 500 all moved
  from AUTH_REQUIRED *"PAT may lack read:user"* to an ERROR that says what
  happened; a 401 and a 403 are untouched, which is what that message is
  for.

- **A healthy call's socket no longer waits for the cyclic collector.** The
  hook that learns the connection replaces `pool._get_conn` with a closure
  over the pool's own bound method, and pool -> closure -> cell -> bound
  method -> pool is a reference cycle: `session.close()` dropped the pool,
  refcounting did not free it, and the connection it held - with its socket -
  lived until a gen-2 collection. Measured over 300 healthy calls against a
  loopback server, **17 established sockets open at once** where plain
  `requests` left none. Bounded and always reclaimed, but until then the app
  held open connections to the four hosts after the refresh that opened them
  had finished, which is the one thing `_new_session`'s docstring says cannot
  happen. The wrapper is taken off again in the same `finally` that cancels
  the timer, before the close: **0 after**, with nothing left for the
  collector to free. That loop is total - each pool comes off under its own
  guard, so one that refuses does not leave the pools after it wrapped - and
  the `finally` now runs the cancel inside the same guarded chain, so the
  un-watch and `session.close()` cannot be skipped by it.

- **`Config.save()` is atomic.** It was a bare `path.write_text`, which
  truncates before it writes. That was tolerable while the only writes were
  the user's own, and 1.3.1+cfa.6 gave the app two reasons to write this file
  unasked - the deferred-purge drain runs from `App.__init__` and from the
  five-minute heartbeat - so a crash or a power cut inside a write nobody
  asked for truncated the file holding every setting plus both pending lists.
  The loader survives that (`config.json.corrupt` and defaults, executed) and
  the settings do not. Same discipline as `secrets.dat` and the meter
  catalog: a temp file in the same directory, `fsync`, `os.replace`, temp
  unlinked on any failure. The helper moved to `atomic_write.py` rather than
  being imported where it was, because `secret_storage` imports `config` and
  so cannot be imported from it. One side effect, pinned rather than left as
  a surprise: the file is now `0600` on macOS and Linux, where a bare
  `write_text` gave `0644` under the usual umask - `mkstemp` creates at
  `0600` and `os.replace` carries that across. Windows is unaffected and
  still relies on the user-scoped `%APPDATA%` location. The exception
  contract does not move: `save()` still raises `OSError`, and the nine
  callers - four of which swallow it, five of which do not - are unchanged.

- **The glob side of the egress guard's case fold has a test.** The deny
  matcher lowers both the pattern and the candidate, because `fnmatch` is
  case-sensitive on POSIX and insensitive on Windows. Only the candidate half
  was covered, and the round-3 confirmation of PR #23 found that removing
  `.lower()` from the *pattern* side survived the whole suite - every
  built-in deny glob is already lower-case, so lowering it again changes
  nothing. `paths.deny` is operator-editable and this repository is told to
  edit it, so an operator's `**/Secrets/**` has to deny `x/secrets/y`. Eight
  parameters, run both ways against `tests/test_egress_guard.py`: with the
  pattern side unlowered, **4 fail and 456 pass** - the four upper-case globs
  and nothing else in the file; with the candidate side unlowered, **5 fail
  and 455 pass** - the four mirrors plus the existing
  `C:\Users\m\.AWS\CREDENTIALS` case that was already there.

### Documentation

- **`SECURITY.md` describes "Clear all browser data".** It covered egress,
  secrets and the Azure rate floor, and said nothing about the one button
  that deletes a directory tree and a set of keyring entries - or that since
  1.3.1+cfa.6 the request is written into `config.json` and carried out at
  the next start. A new heading says what it removes and for which accounts,
  that the cookie secrets go at the click while the profile directories are
  handed to the app (Qt requires a profile to outlive its pages, so one being
  refreshed is deleted when that refresh finishes), and that the deferral is
  a list of account ids only - bounded at 64 entries of 64 characters, never
  a cookie or anything from a provider page, with a symlink never followed or
  deleted through. The "written atomically" paragraph now covers
  `config.json` as well.

- **`SECURITY.md` describes the transport it now has.** The sentence about
  Azure traffic said "plain `requests` with a 15-second timeout, like Copilot
  and OpenRouter" - true of one socket operation, and incomplete about
  everything this release added. It now names the per-socket timeouts (15 s,
  10 s on Copilot's `/user`), the 30-second deadline on the whole exchange
  and how it is enforced, the 8 MiB response ceiling, the refusal to follow
  or to misreport a redirect, and that nothing retries.

- **Three comments and a docstring that had drifted.**
  `Config.save()` now says that the file lands `0600` on macOS and Linux and
  why, and that a symlinked `config.json` is replaced rather than written
  through (pinned by a test on both counts, the second one new).
  `azure.REFRESH_DEADLINE_SECONDS`' justification quoted "~13 min" for 50
  unguarded page calls, which was the arithmetic at a per-socket timeout;
  at a whole call it is ~37 min, and the comment says what the deadline is
  for rather than restating a number that moved. `providers/catalog.py`
  imported the private `secret_storage._atomic_write` inside a function, to
  keep `ctypes.wintypes` out of a non-Windows startup - a reason that expired
  when the helper moved to `atomic_write.py`; it imports the public
  `atomic_write` at module scope, and importing the catalog no longer pulls
  in `secret_storage` at all.

### Notes

- The suite is **1 859 tests**, from 1 729. `tests/test_http.py` is new and
  holds 85 of them; the Copilot file is at 34, OpenRouter's at 38, Azure's at
  232, the config file at 138, and the egress guard's at 460. Ninety of
  the hundred and thirty came from the review rounds. Round 1's
  thirty-six: the out-of-band deadline (12), the urllib3 floor and the
  nested-coding refusal (5), Copilot's named transport failures (2), the
  redirect refusal (10), the five mutation survivors the code lane found (6)
  and the symlinked-`config.json` note (1). Round 2's thirty: the timer's
  re-arm (4), the adapter hook driven from `requests` itself (4), the
  un-watched pool (1), the comma refusal (9 with its parameters), the 304
  (3), Copilot's truncated reply (2) and four edges that survived the code
  lane's mutations (7). Round 3's eighteen, less the one they replaced: the
  detached socket a handshake leaves behind (1), Copilot's username rule (10
  with its parameters), the missed-hook line's meaning (1), the timer that
  cannot start (2), the un-watch that must not mask a call's failure (2),
  and two the mutation runs walked through - the re-arm interval against the
  read timeouts, and the count inside the refusal message. One existing test
  also had its derivation completed rather than left with a magic constant
  in it. The confirmation pass's seven, all of them pins on lines round 3
  added: a pool with `__slots__` against the un-watch's `__dict__` default
  (1), `DeadlineUnavailable` in `HELPER_EXCEPTIONS` and on Copilot's tile
  (2), the un-watch guard's breadth against an `AttributeError` (1), the
  un-watch loop finishing past a pool that refuses (1), a `cancel()` that
  raises leaving the session closed (1), and the re-arm being armed before
  the missed-hook line is written (1).
- **The hook the whole bound rests on is now driven by `requests`.** Every
  other transport test injects at `Session.request`, which is above the
  adapter, so `_ConnectionRecordingAdapter.get_connection_with_tls_context` -
  a `requests` 2.32 method feeding urllib3's private `_get_conn` - was
  covered by nothing: renaming it left the suite green while a TLS header
  drip went from 3.00 s to unbounded - still inside the call when the harness
  gave up watching. (An earlier draft said "14.03 s and stuck" there, which
  was one harness's give-up rather than a bound.) Two tests now run
  `requests`' own send path with the urllib3 pool faked below the adapter,
  and when the deadline comes due with no connection recorded at all the
  timer writes one `provider http deadline_hook_missed=True` - a fixed
  literal - so a future `requests` that moves the seam shows up somewhere
  rather than nowhere. A call that merely has no socket yet, which is what
  a slow resolver looks like from there, says nothing: writing the line for
  that too made the signal unreadable on exactly the networks it matters on.
- Not one `responses.add(...)` in the existing provider tests needed editing.
  `responses` supports `stream=True` and hands back a real urllib3 handle, so
  the whole transport change is invisible to them - which is the point.

## 1.3.1+cfa.6 - 2026-09-15

The residuals of the refresh-cadence release, and two coercions on
`config.json`. A healthy provider's cadence does not change: over six fake
hours beside a wedged REST provider it is dispatched 11 times where the same
run with nothing wedged dispatches it 12, and over a day 29 against 30. What
moves is the park on a hung REST worker, where profile deletion is decided
and recorded, the name an answer is filed under, and what a log line is
allowed to cost.

### Changed

- **A REST provider's park is released by its worker, not by a timer.** A
  provider the watchdog gave up on was parked for twice the budget that
  expired and then let go. For the browser providers that is right: the
  account-keyed live-scrape guard refuses the re-entrant scrape anyway.
  Copilot, OpenRouter and Azure have no such guard, and `requests`'
  `timeout` is per socket operation rather than a total, so a server that
  drips a byte just inside it holds a `QThreadPool` worker for as long as it
  likes — and every park expiry started another one on the same endpoint.
  Six fake hours against a wedged REST worker, with the app in its active
  five-minute cadence — something on screen is moving, which is when the app
  is busiest and the accumulation is worst: **50 dispatches under the old
  rule, 6 under this one**, no two closer than 3 680 s. An *idle* app backs
  off to an hourly cadence of its own, so the same six hours are 11 and 6
  there, and over 24 hours 28 and 24: the bound matters most exactly when
  the app is working hardest. Each of those 50 is a
  slot of the *global* pool, which fills (1 of 1, 2 of 2, 4 of 4, 8 of 8),
  after which all three REST tiles are dead for the life of the process. The
  park now lasts until the worker reports back — any snapshot for that name,
  live or late, already un-parks it — or one hour, whichever is first;
  browser providers keep the 2x ceiling, measured unchanged at 26 and 28
  dispatches over the same six hours. The `abandoned` log line says which
  rule applied. A retry due that comes up during such a park still rides the
  next ordinary cadence wake rather than buying one of its own: pinned over
  eleven five-minute wakes inside one hour-long park.
- **A manual refresh that is refused now says so on the tile.** Refused as
  `abandoned`, both manual routes re-enabled and did nothing visible; with an
  hour-long park that silence is long enough to read as a broken button. The
  tile's status tooltip — and its status text, where that text is empty —
  says "Waiting for the previous refresh to finish." An ERROR or
  AUTH_REQUIRED tile keeps its own label, which is the one thing on it the
  user can act on. Nothing else moves: no snapshot, no history, no ratio, no
  cycle, and the next paint clears it. A scheduled cycle marks nothing.
- **"Clear all browser data" hands the profiles to the app.** The dialog
  cleared the cookie secret and then called `purge_profile` synchronously for
  every configured account, every profile directory on disk and the three
  fixed ids, consulting nothing. That is `deleteLater()` on the cached
  `QWebEngineProfile` followed by `rmtree`, Qt requires a profile to outlive
  its pages, and the dialog is modeless with a cycle running every five
  minutes — so a live scrape during that click is ordinary, and this was the
  most reachable route to the destroyed page that used to strand the
  live-scrape guard for the life of the process. The stored cookies are still
  cleared at the click, for every id. The profiles now go to the App, which
  deletes each one as soon as that account is free, and the confirmation says
  that a profile being refreshed right now is deleted when that refresh
  finishes or at the next start.

  These ids are kept on their own list, `config.pending_data_clears`, rather
  than in `pending_profile_purges`: that list's drain skips an id that is
  *also* a configured account, which is right for a removal a restored backup
  has undone and would silently drop every deferred clear at the next start,
  because a clear is *always* about an account the user still has. Both lists
  are persisted and both are drained at the next start — before any cookie is
  hydrated and before any provider exists — through one helper and one
  `Config.save()`; only the skip rule differs. The clear list has to be
  written down: the keyring copy of the credential is deleted at the click,
  so a quit inside the deferral window used to leave the account's
  `ForcePersistentCookies` profile — the live session cookie, which is what
  the button exists to destroy — on disk with nothing in the app ever
  mentioning it again. `docs/next-session.md` §8.3 records that this is the
  second thing that makes the app write `config.json` unasked.

### Fixed

- **A parked provider no longer freezes the idle backoff.** `_begin_cycle`
  filtered the parked names out and *then* decided whether the cycle was
  partial, so every cycle inside an hour-long REST park counted as one —
  and `_end_cycle` will not advance `_unchanged_cycles` for a partial cycle,
  which is what `_adaptive_refresh_minutes` derives the idle interval from.
  One hung endpoint therefore pinned the whole app on the five-minute active
  cadence for as long as it stayed hung, multiplying the scrapes of *other*
  providers' hosts: a healthy sibling went from 12 dispatches in six fake
  hours to **54**, and with every tile's number moving every three hours —
  the realistic case, because each move zeroes the counter — from 68 in a
  day (2.83/h) to **154** (6.42/h). Partial is now decided from what the
  cycle was *asked* for, which is what `_end_cycle`'s comment always said it
  meant: a retry wake or a per-provider refresh. The numbers go back to 11,
  29 and 67, against controls of 12, 30 and 68.
- **A park uses the rule the dispatch went out under.** `_on_watchdog` asked
  `_providers` whether the name is a browser provider, and a settings save
  that removes an account drops its provider object while the scrape is
  still out — so a *browser* account removed mid-scrape was parked for an
  hour under `ceiling=rest_backstop`. Its on-disk profile, which holds the
  session cookie, then waited 60 minutes for deletion instead of 10, the
  same account re-added was refused for the rest of that hour, and the log
  line named the wrong rule. The kind is recorded with the epoch at dispatch
  and pruned with it.
- **The rest of that log call cannot raise or bloat either.**
  `_raw_keys_for_log` is evaluated in the same `log.warning` as
  `_raw_summary` and guarded only per key, the call site's own
  `if snapshot.raw` ran the payload's `__len__`, and both "bounded literal"
  fallbacks embed a class name the payload chose (1 MB in, 1 000 017
  characters out; now 77 in app.py and 60 in the scraper). `snapshot.error`
  on that record is clipped to 300 characters with its newlines flattened: a
  2 480 000-character error — 20 000 forged lines — made one record of
  2 480 065 characters with `provider=copilot` and 2 480 069 with the
  longest provider name, 1.58x the whole 512 KiB × 3 rotation, every one of
  those 20 000 lines reading like a real one. (Every multiplier in this
  entry is against the whole rotation, 512 KiB × 3 = 1 572 864 bytes. An
  earlier draft divided by one 512 KiB file and read 3× worse: 4.73x here.)
  The tile, the tray tooltip and the error dialog still get the string
  whole. `scrape fail`'s `load_error_string` is clipped the same way;
  measured against a real QtWebEngine it is a Qt string-table message
  rather than the server's, so that one closes an assumption rather than a
  hole. All three arguments are guarded, `snapshot.error` included:
  `UsageSnapshot` is a plain dataclass, so `error or ""` runs the object's
  `__bool__` and `str()` its `__str__`, and either raised straight out of
  `_on_snapshot` — before the tile was painted — until the helper caught it
  and answered `<unprintable error>`. The clip runs *before* the redaction rather than
  after, so four regex passes see 500 characters rather than the whole
  string (0.08 ms for a 1.2 MB error against 163 ms): the margin past the
  300-character limit keeps an identifier straddling *that* limit whole for
  the redaction, and a cut identifier left at the end of the 500-character
  window is dropped after it, because redaction shrinks the text in front of
  such a fragment and pulled 26 characters of a subscription id into the
  record. Swept across every offset the window can cut an id at, no run of
  eight hex-or-dash characters of it now reaches the log. The scraper's
  `_result_keys_for_log` gets the same outer guard as app.py's twin, because
  `for key in result` runs the payload's `__iter__` and a `dict` subclass
  can refuse it.
- **A short id with a newline in it cannot forge a log record.** The
  coercion on both pending-purge lists bounds an id's type and its length,
  not its characters, and four records print ids through `_clip_for_log`. A
  53-character id from a hand-edited `config.json` carrying two newlines
  read as three records in the file — a forged `ERROR … balance=0.00
  key=sk-ant-x` among them. Both a carriage return and a newline, because a
  bare `\r` makes a record overwrite the one before it. Flattened exactly as
  `snapshot.error` is: 6 forged lines to 0 on the same poisoned config, and
  the helper that does it is guarded end to end like the three beside it —
  its `str()` was the last one on that record outside a `try`.
- **Smaller ones.** A settings save no longer writes "Waiting for the
  previous refresh to finish." onto a parked tile — it refreshes without the
  user having asked, exactly like a scheduled cycle — while a sign-in queued
  behind such a save still does, which it did not: the queued route recorded
  the provider and not that a person had asked, and the save's own queued
  refresh won. An id owed both a removal and a clear reaches `purge_profile`
  once across the two calls one dialog session makes, and one `deferred` line
  per drain rather than two while its scrape is still out. "Clear all browser
  data" counts the `profiles/` directories `purge_profile` will refuse — by
  the rule it refuses them with, the resolved path and not the name alone —
  and says how many, without naming them. A **symlink** is never one it will
  act on, whatever it resolves to: one pointing out of `profiles/` was
  counted as removed where the purge refused it, and one pointing at another
  profile *inside* it passed containment and deleted the account it aliased,
  under a live scrape, because the deferral is keyed on the link's own name.
  `purge_profile` itself refuses a link too, so an id that reaches it from
  `config.json`'s two pending lists cannot delete through one either.
  An entry resolving to the `profiles/` root is refused for the same reason —
  `webview_profile_dir` permits it and `purge_profile` does not. Measured on
  a hostile tree of eight entries: the predicate disagreed with the deletion
  on five of them and now on none, the emitted set drops from six ids to
  three, and the box's "left alone" count goes from 2 to the 5 that really
  were. Its keyring pass still takes every name on disk: only the directory
  sweep has a containment question to answer.
- **Neither purge path asked whether a scrape was live.** Both now consult
  `account_is_busy()` from `providers/_scrape_runner.py` as well as
  `_inflight` and the abandoned-dispatch park. It is the only one of the
  three that still answers yes once the App has stopped waiting for a
  dispatch or never made one, because it is module state keyed by account and
  survives the `_build_providers` a settings save runs.
- **The App stamps the name it dispatched onto every answer.** `_dispatch`'s
  `_emit` forwarded the provider's own `snapshot.provider`, and every gate
  downstream keys on that name while epochs advance in lockstep across a
  cycle — so an answer mislabelled with a *sibling account's* id was accepted
  as that sibling's live answer: its in-flight entry cleared, its watchdog
  destroyed, its tile painted with another account's numbers. Unreachable
  today, and checked rather than assumed (`ScrapeRunner` sets
  `provider=self._account_id`, the browser builders take `account_id=` from
  the App, the three REST providers hardcode their literal), but it is one
  line in the one place that knows what it dispatched: `_emit` compares the
  payload's name with the dispatched one and re-stamps it with
  `replace(snap, provider=_name)` when they differ. The warning it logs names
  the dispatched provider and a fixed literal; the payload's own string is
  never printed.
- **The log summariser can no longer raise or print an unbounded value.** Its
  shared character budget covered strings, key names and elided nodes; the
  numeric branch charged a flat 8 whatever the magnitude and the `repr()`
  fallback was charged after the fact and never clipped. Measured, and then
  measured again after: a 5 MB `bytes` value **5 000 012 → 312** characters
  (3.18x the whole rotation, to a fifth of a line), a 5 MB `bytearray`
  5 000 023 → 312, a 50 000-element `set` 338 899 → 312, fifty 4 200-digit
  integers **210 440 → 50**. An int's printed length is now estimated from
  `bit_length()` and the number is never converted: CPython 3.11+ raises on
  `str()` past 4 300 digits and `json.dumps` hits the same limit from the
  inside, so asking how long it is was itself the crash — one 6 000-digit
  integer raised `ValueError` straight out of `_on_snapshot`. So did an
  object whose `__repr__` raises, a dict key whose `__str__` raises and a
  `dict` subclass whose `items()` raises; `except TypeError` caught none of
  them. `_raw_summary` now catches `Exception` — a log line must never be
  able to raise — and its fallback is a bounded literal, where `repr(raw)`
  handed back the whole payload on the one path that had already gone wrong.
  The existing adversarial payloads are unmoved: the worst `raw_summary=`
  argument is 4 803 characters and the whole `snapshot error …` record it
  sits in is up to 4 998 — the record carries the provider's name, so it is
  4 992 for `azure` and 4 998 for `opencode_go` (`_nested(20, 4)`, measured
  on both trees; an earlier draft of this entry, and the message of commit
  `21bd5be`, gave 4 283 and called it the record — the claim was right, the
  number was not, and 4 803 is the argument rather than the line). None of it is reachable today; it bites the first time an
  extractor or a provider returns something that is not plain JSON.
- **The scraper's log lines clip the text the page chose.** `title=%r` at
  four call sites and `result_keys=%s` at one had no length cap, and the
  healthy `scrape ok` line is at INFO. Driving `_finish` with a 1 MB
  `document.title` and an extractor result of 10 000 keys of 1 000
  characters: **11 079 134 characters for `scrape ok` and 1 000 367 for
  `scrape fail`** — 7.04x and 0.64x the whole rotation, from one scrape, into
  the file the error dialog asks the user to attach. The same inputs now
  produce 3 664 and 570 (3 814 until `_key_text(key)[:60]` stopped appending
  the `"..."` marker to each of 50 over-long key names). Titles clip at 200; the key list takes the shape
  `raw_keys=` already has, 50 names of 60 characters and the true count
  beside them. `_safe_url` was checked and was already bounded at 300.
  Nothing else in the scraper moves.
- **Two coercions on a poisoned `config.json`.**
  `pending_profile_purges` now keeps only strings of 1 to 64 characters and
  at most 64 of them — `purge_profile` is still the defence that matters,
  but a delete list read before anything else at startup should not be able
  to carry 5 000 ids of 200 000 characters to it. And a `BrowserAccount` id
  may no longer be `copilot`, `openrouter`, `opencode_go` or `azure`: those
  are exactly the provider keys `_build_providers` creates that are not
  browser accounts, and it keys one dict on both, so such an account owned
  that provider's snapshot, tile and place in the queue. `claude` and `codex`
  are not on the list — they are the two fixed browser accounts. A config
  carrying one still loads: the migration drops that account, as it already
  did for an unsafe path component, and now logs it with the id bounded to
  64 characters.
- **A park no longer outlives the provider it was about.** Nothing cleared
  `_abandoned` for a name the user removed in Settings — `_dispatch_refusal`
  answers `not_configured` before it ever asks — so the entry and the
  per-dispatch rows held open for it lived for the process.

### Notes

- **1 216 → 1 277 tests.** Including six fake hours of a wedged REST worker
  against a browser sibling, an hour-long park ridden out over eleven cadence
  wakes, a mislabelled answer that must not touch its sibling's dispatch, and
  the three payloads that used to raise out of `_on_snapshot`. **Thirty-six**
  of them are this release's own review, over three rounds: the idle backoff
  measured with a provider parked and without, a removed browser account's
  park, the epoch guard on the un-park, a deferred clear carried across a
  quit and the payloads that refuse to be iterated, measured or repr'd
  (round 1); the snapshot-error record bounded where it is written, a
  sign-in queued behind a settings save, and an id on both deferral lists
  purged once (round 2); and a subscription id swept across every offset the
  log's clip can cut it at, a link in `profiles/` that is never a profile,
  and the two arms of `is_usable_profile_id` the filesystem decides
  (round 3). Counted by collection: 1 216 at `origin/main`, 1 239 when the
  release was tagged, 1 275 after the three review rounds, and 1 277 once
  `main`'s suite-teardown fix (#26) was merged in and `purge_profile` learned
  to refuse a link.
- **The REST socket itself is still unbounded.** This bounds how many workers
  a hung endpoint can accumulate, not how long one of them lives. A total
  response deadline — `stream=True` plus an elapsed check while reading — is
  the only thing that bounds the socket, and it is still the open item in
  `docs/next-session.md` §8.3.

## 1.3.0+cfa.5 - 2026-09-15

The refresh cycle. Four and a half days of a real desktop log — 137 cycles,
2026-09-10 to 09-15 — were read before any of this was written, and the
numbers in the entries below are that log's, not estimates.

### Changed

- **The error fast-retry is per provider.** It was cycle-wide: any ERROR
  snapshot anywhere meant the *next cycle* ran in one minute, for every
  provider. 124 of the 137 cycles contained at least one ERROR or
  AUTH_REQUIRED — OpenCode never succeeded once in the whole run, 95 auth
  failures and 38 errors — so a third of all cycles started within two minutes
  of the previous one and five healthy providers were re-scraped because of one
  broken tile. Claude alone costs a median of 18.5 s and a p90 of 48 s of
  browser time per scrape, so the app was very nearly always refreshing. Then
  the bound bit the wrong way round: past three failing cycles *nobody* got a
  fast retry, and since no cycle was ever clean the counter never reset, so a
  genuinely transient failure elsewhere got nothing.

  Each provider now carries its own consecutive-error count and its own due
  time. An ERROR schedules that provider's retry at 1, 2 then 4 minutes and
  then falls back to the normal cadence; OK clears it; AUTH_REQUIRED clears it
  too, because signing in is the user's move and retrying it quickly only
  burns page loads. A wake that is only a retry refreshes **only the providers
  that are due**, not the whole queue.

- **The cheap providers no longer queue behind the browsers.** Copilot,
  OpenRouter and Azure are plain HTTPS calls on a thread pool; only the
  refresh queue made them wait. Cycles ran a median of 48 s, a p90 of 79 s and
  a maximum of 100 s, nearly all of it browser time, with Copilot's payload —
  about a second's work — landing near the end of each one. They are now
  dispatched together at cycle start. The browser providers stay strictly
  serial: QtWebEngine is GUI-thread-only and each scrape holds a profile.
  Copilot also joins OpenRouter and Azure at the head of the queue.

- **The cadence signature follows the snapshot's status, not its error text.**
  Azure's fail-closed message counts a minute down — "Waiting for the next
  Azure fetch window (43 min)" — so it differed on every cycle, which read as
  "this provider changed", re-armed the 30-minute active window and zeroed the
  idle backoff for every provider, on a clock. `unchanged_cycles` was 0 in 55
  of 204 heartbeats and only ever reached 9–12 overnight, so the documented
  idle backoff almost never engaged. The message still reaches the tile, the
  tooltip and the log; it just no longer decides how often the app polls.

### Added

- **The scheduler logs what it does.** It logged nothing at all, so the cycles
  in that desktop log had to be *inferred* from runs of provider activity
  separated by 45 s of quiet, and "I clicked Refresh and nothing happened" was
  unprovable either way. There are now lines for the start of a cycle (manual,
  reason, providers), its end (duration, changed, errors, auth_required), the
  next scheduled wake **and the reason that chose it** (active, idle,
  error_retry, reset_pull_forward, manual), a queued or ignored manual
  refresh, and a start/done pair per provider turn. The heartbeat prints the
  per-provider retry state, which was computed and then dropped by its own
  format string. Azure's cached path and OpenRouter's healthy path were
  `debug`, which the file handler drops — between live fetches neither
  provider left any trace at all — and are now `info`.

- **A watchdog on every dispatch, and an epoch to go with it.** `_inflight`
  was cleared only by an arriving snapshot, and the scheduler returns early
  while it is non-empty, so one provider that never called back stopped every
  future refresh until the app was restarted. Each dispatch now has a deadline
  taken from the provider itself — the browser providers derive it from their
  scraper timeout times the attempts they may make, Azure reports its real
  worst case (its page-loop deadline **plus** the fixed calls that sit outside
  that budget, because `REFRESH_DEADLINE_SECONDS` is a floor on a refresh's
  ceiling rather than the ceiling), and a REST provider gets a flat 60 s plus
  an allowance for time spent queued on the shared thread pool, since its
  deadline starts at dispatch and its work starts when a pool thread frees up.
  After that the App gives up on that provider, records the failure and
  carries on.

  Giving up ends the App's wait, not the provider's work, and the rest of this
  entry is about not pretending otherwise. Every dispatch carries an epoch, so
  an answer is matched to the dispatch that earned it: one from a dispatch the
  App abandoned is logged, paints and records only its own tile, and closes no
  cycle. A provider the
  watchdog gave up on is **parked** — no cycle, retry wake, manual refresh or
  settings save dispatches it — until its worker reports back or twice its
  budget has passed and the worker can fairly be assumed dead, and the three
  browser providers refuse a re-entrant refresh outright, so the one case that
  ceiling lets through still cannot open a second `QWebEngineView` on an
  account's one profile. Without this, a provider that stopped answering
  accumulated live scrapes: up to 19 of one provider in a six-hour simulation,
  all sharing one cookie store.

  A cycle also accounts only for what it dispatched; a snapshot from a
  provider outside it repaints its tile without joining that cycle's progress,
  error count or "changed" verdict.

  The heartbeat restarts a refresh timer that is not running, and ends a cycle
  that is open with nothing in flight, nothing queued and no watchdog left.

- **A visible refreshing state.** The header said `· active next now` for the
  whole cycle: `set_refreshing` wrote "refreshing…" and the 1 Hz label tick
  overwrote it within the second, while the countdown it rewrote from was
  stale and already in the past. The refreshing state is now durable, counts
  the cycle off ("· refreshing 2/6"), and scheduled cycles mark their tiles
  too — lightly, since nobody asked for them — where before a scheduled
  refresh showed nothing at all. The tray updates per snapshot rather than at
  cycle end, so the dot and its tooltip stop being a whole cycle behind the
  tiles.

### Fixed

- **A manual refresh during a cycle is no longer discarded.** `refresh_now`
  and `refresh_provider` both returned early while anything was in flight, the
  Refresh button is disabled for the whole cycle, and the tray menu's "Refresh
  now" was therefore a no-op — at exactly the moment a tile looks stale, which
  is usually mid-cycle. Saving settings mid-cycle had the same hole. The
  request is queued and runs once when the cycle closes.
- **A snapshot for a provider you just removed no longer re-creates its
  tile.** A settings save rebuilds the providers while a refresh is still out,
  and the late snapshot came back through `ensure_tile`.
- **Changing the Copilot quota or the OpenRouter budget mid-refresh no longer
  throws that refresh away.** Both re-render the cached snapshot immediately
  so the new denominator shows without waiting for a network call, and both
  did it by handing the scheduler a snapshot with no dispatch identity. The
  scheduler read it as that dispatch's answer: it cleared the in-flight
  entry, destroyed the watchdog, wrote `refresh provider done … status=ok`
  for a dispatch that had not answered, recorded the cached value as the
  cycle's result and could close the cycle — after which the refresh the
  settings save itself starts could put a second worker beside the first,
  and the real answer was discarded as late. A re-render is now a repaint:
  it touches the tile and nothing else.
- **A settings save no longer lets a second browser scrape onto one
  profile.** The "a refresh is already running" refusal read a field on the
  provider object, and saving settings — a colour-only change included —
  replaces that object. Past the point where the app stops assuming an
  abandoned worker is still alive, nothing refused. Measured on the real path
  — the real `ClaudeProvider`, the real runner and a real
  `_build_providers()`, with only the headless scraper faked — six fake hours
  with a wedged scrape and a save every 400 s (55 provider rebuilds) put **29
  headless views on one account's single browser profile**, all writing one
  cookie store, which is how a spurious sign-out happens. With the refusal
  keyed on the account: **one view and 11 refusals** over the same six hours.
  A provider object is also reused when nothing about it changed.

  An entry in that registry expires. It is cleared when the scrape reports
  back, and `HeadlessScraper` arms its own timeout so it always does — but
  that invariant lives in another module, and a `_finish` whose diagnostics
  raised before its emit (a page whose C++ half Qt had already deleted, which
  is what destroying a profile under a live scrape produces) skipped it. The
  account was then refused for the life of the process, with no settings save
  able to clear it, because not being clearable by a rebuild is the point of
  module state. The diagnostics in `_finish` now give way to the signal, and
  an entry older than the scrape's own worst case — the runner's timeout
  times the attempts it may make, plus a minute — expires with a line in the
  log.
- **A provider that answers late is no longer stuck on "Refresh timed
  out."** Dropping a late answer whole is right for the cycle's accounting
  and wrong for the tile: a provider that is merely slower than its budget
  answered correctly every time, and a genuine "sign in again" was never
  shown. Its own tile now gets the answer when it is the newest dispatch's,
  and the fast-retry entry is cleared only if it answered OK or
  auth-required. It is **recorded** as well as painted: it is a real
  observation, and the only thing the history and burn-rate stores had heard
  about that dispatch was the watchdog's synthetic timeout, which both of
  them drop — so a provider slower than its budget showed a healthy tile
  above an empty ratio history and a blank burn-rate row (0 rows over twelve
  cycles, against 12 for the same provider inside its budget). Nothing else
  moves — no cycle is closed or joined, no scheduling of any kind, and no
  newer dispatch is touched.
- **A profile deletion deferred past a quit is no longer lost.** Removing an
  account while a refresh of it is still out defers deleting its browser
  profile, because Qt cannot free a profile under a live page. The list was
  in memory only and the account is gone from the config by then, so quitting
  inside that window left a removed account's persistent cookie store on disk
  with no recovery path short of "Clear all browser data". What is still owed
  is now recorded and run at the next start, before anything can open a page.
  The stored credential was, and is, cleared immediately. The drain checks
  membership as well as ordering: an id that is *also* a configured account —
  which a restored backup, a synced config directory or a hand-edited undo can
  produce, since the list now outlives the removal that wrote it — is skipped,
  logged and dropped rather than deleting a live session. Its opening line
  names a count with a bounded sample of ids beside it, and `purge_profile`
  clips the id it refuses: the list is config-controlled and unbounded, and
  echoing it whole let a poisoned `config.json` write 800 KB of records at
  every start, one line of it 0.76× the whole 512 KiB rotation.

  Note that this is also the first path on which the app rewrites its own
  `config.json` without the user asking: `_run_profile_purges` records what is
  still owed, so while a purge is deferred the five-minute heartbeat can write
  the settings file. It early-returns when nothing changed, so the steady
  state writes nothing.
- **A fast retry owed to a provider the app has given up on is no longer
  spent for nothing.** Its deadline was consumed before the cycle filtered
  it out, so the retry vanished and — when it was the only one owed — the
  wake ran a completely empty cycle, header flicker included. The deadline is
  now kept, and owed no earlier than the next ordinary cadence wake: arming
  it for the instant the park lifts bought a wake of its own, which is one
  extra dispatch an hour for a provider that is hung, and for the three REST
  providers — which have no re-entrancy guard of their own — one more worker
  holding a slot of the global thread pool.
- **A refresh with nothing eligible no longer opens a cycle.** Every entry
  path filters out what it cannot dispatch and then opened a complete cycle
  over what was left, even when that was nothing: the header blinked
  "· refreshing" with no fraction behind it and two log lines claimed a cycle
  that dispatched nobody. A *manual* one cost more, because the active-window
  re-arm sits after the filter and never asked whether anything survived it —
  clicking Refresh while the only provider was parked pinned the app on the
  fast cadence for thirty minutes and threw away the idle backoff, in
  exchange for zero network calls.
- **A provider that is only waiting can no longer spin the scheduler.** A
  `throttled` or `resume_artifact` answer skips the fast retry, and the skip
  used to leave an already-owed retry deadline in place — now in the past. A
  past deadline is clamped to *now*, the wake timer has a 1 000 ms floor, and
  the wake produces the same answer: a self-sustaining refresh cycle at about
  1 Hz for as long as the wait lasts. Driving the real Azure provider for an
  hour measured 3 541 cycles where 12 were intended, by two ordinary routes —
  an offline burst followed by a settings change on an Azure query field, and
  a watchdog giving up on a wedged fetch with no user action at all. It costs
  no extra requests, but it writes about 3.8 MB an hour into a log that keeps
  1.5 MiB, so `ai-gauge.log` — the file the error dialog asks you to attach —
  was overwritten every eight minutes. A retry deadline is now spent when it
  is dispatched rather than when an answer happens to clear it.
- **A settings save that removes a queued provider no longer stalls the
  app.** Removing a provider that was still queued behind a running browser
  scrape left the cycle open forever: the timer stopped, the Refresh button
  disabled, the heartbeat's recovery blocked, and a queued manual refresh
  stranded, until a restart. Only the tray menu could get out of it.
- **A retry wake that finds nothing due no longer refreshes everything.** The
  due test is exact, and a Qt coarse timer may fire a few milliseconds early,
  so the wake one provider's retry bought could fall through to a full cycle —
  the cycle-wide retry this release removes.
- **A removed account's browser profile is deleted by the app, not by the
  settings dialog, and not while a refresh of it is still out.** Deleting a
  QtWebEngine profile under a live page is unsupported by Qt, and the
  surviving page can write rotated session cookies back into the directory
  that was just removed. The stored credential is still cleared immediately.
- **Watchdog timers are destroyed, not just stopped.** Each one was parented
  to the app object, so 2 000 dispatches left 2 000 live timers in a process
  designed to run for weeks.
- **A timeout measured across a machine suspend is not counted as a provider
  failure.** A laptop resumed after two days reported `elapsed_s=228477`
  against an 80 s budget; every resume cost one spurious failure per provider,
  and each of those armed the fast retry. Such a timeout is logged as
  `classification=resume_artifact` and skipped by the retry. The scrape still
  fails — the timing itself is unchanged.

### Notes

- **Azure's own throttle is untouched.** Its fail-closed "waiting for the next
  fetch window" and "a fetch is already in progress" snapshots now carry a
  one-word error class so the scheduler does not read a provider that is
  deliberately not fetching as a provider that failed. The hourly floor, the
  backoff and the cache are exactly as they were.
- **`BrowserAccount.enabled` is still not honoured, deliberately.** An
  earlier draft of this release made `_enabled_providers` and
  `_build_providers` read the field. Nothing in the app writes it: the
  settings dialog sets it once when adding an account, and the only other
  writer is the config migration, which stamps `bool(providers.<kind>)` when
  it inserts a missing fixed account. A config migrated while
  `providers.claude` was false would therefore carry `enabled: false`
  forever, and the Settings checkbox - which flips `providers.claude`, not
  this - could never undo it. Reading a field nothing writes turns that
  checkbox into a permanent no-op, so the change was reverted. The field is
  still parsed, so an existing `config.json` loads unchanged.
- **Metric labels still count toward the cadence.** OpenRouter's
  `Today ($3.10/$5.00)` and Copilot's `Credits (12.5/1500)` can still read as
  a change. Hashing a stable `key` instead is the same repo-wide decision that
  `history` and `ratio` are waiting on, and it does not belong bundled with a
  cadence fix.
- **Log lines a provider chooses the contents of are bounded.** Promoting
  OpenRouter's healthy path from `debug` to `info` put an unbounded list of
  response key *names* — chosen by the server — into a rotating 512 KiB × 3
  log on every refresh, where one hostile response is a megabyte-long record
  and three discard the whole diagnostic history. Both OpenRouter lines now
  print the first twenty names, clipped, plus the true count. Both fields of
  the snapshot lines are capped too, and the second of them needed more than
  a key cap: `raw_summary` bounded the number of keys, the length of values
  and the depth, but not the length of a key *name* and not the total, and
  those per-node caps multiply - fifty keys at each of three levels is
  125 000 nodes. Measured against `snapshot.raw` as a browser extractor
  returns it: 2.2 MB for a fan-out of 20, and 4.77 MB for an api-capture
  shape that stays inside `api_capture.js`'s own limits - 9x the whole
  rotation, from one ERROR scrape. It now clips key names and spends one
  shared character budget across the walk, which puts the same payloads under
  5 KB. Nothing a provider page returns can name its own `error_class` any
  more either: the scraper boundary allowlists the one value it is allowed to
  set.
- **1 099 → 1 216 tests.** Every finding from all three review rounds has a
  regression test, including two invariants driven over a fake clock: an hour
  of any provider behaviour buys a bounded number of cycles, and six hours of
  fuzzed cycles, watchdogs and manual refreshes never puts two scrapes of one
  browser account in flight together. The seven behaviours a mutation run
  could still reverse with every test green — the cycle's books after a
  skipped provider, the per-cycle replacement of the thread-pool allowances,
  the tray update inside a repaint, the deliberate *absence* of recording in
  a re-render, the pending-purge validator, the drain's position ahead of
  cookie hydration, and the exact-class check that makes provider reuse safe
  — are pinned too.
- **Two tiles in that log were broken by configuration, not by the app.**
  OpenCode was not signed in and Copilot's PAT lacked `read:user`. Neither is
  fixed here; both now cost less, because a provider that keeps failing is no
  longer everyone's problem.

## 1.2.0+cfa.4 - 2026-09-10

Adds a **Microsoft** section: Azure, Foundry, and Copilot under one heading.
Copilot is unchanged and simply moves under it. Azure is new.

### Added

- **Azure month-to-date spend tile.** Reads Cost Management for the current
  allowance period and shows spend against a monthly allowance, decomposed
  into component rows by Azure service. Off by default; it needs an Entra ID
  app registration before it can report anything.

  Authentication is OAuth2 client credentials against
  `login.microsoftonline.com`, with no MSAL dependency — the flow is one form
  POST, and every other REST provider here already speaks plain `requests`.
  The client secret lives in the OS credential store; the tenant, client, and
  subscription IDs live in the config file and must all be GUIDs.

  The app registration needs **Cost Management Reader** (cost queries,
  forecasts, budgets) and **Reader** (to list resources and read the
  subscription's offer) on the subscription.

- **Foundry as a roll-up row, not a tile.** Microsoft Foundry (formerly Azure
  AI Foundry) bills per token to the Azure subscription, so its spend is
  already inside the Azure total. A second tile would show the same money
  twice with nothing in the layout saying so; a row inside the Azure tile
  makes the double-count structurally impossible, because every cost row lands
  in exactly one bucket and the buckets sum to the query total (to within float
  rounding).

  Foundry resources are identified by resource **kind** (`AIServices`), never
  by resource type: Azure OpenAI, Speech, Vision, Language and Foundry all
  share `Microsoft.CognitiveServices/accounts`, so a type filter would sweep
  all of them into the Foundry row. Service *names* are only ever used to
  label rows — they already shifted once, when "Azure AI Services" became
  "Foundry Tools". Resources can be pinned by ID in Settings for an app
  registration that lacks the Reader role.

- **Optional rows.** A forecast row when Cost Management has enough history to
  produce one (omitted, not errored, when it does not), and a "Marketplace
  models" row behind a settings toggle, which costs a second query.

- **Allowance from a real Azure Budget when one exists.** Leave the Settings
  allowance at 0 and a monthly cost Budget on the subscription is used
  instead, so the figure does not have to be kept in two places. A typed
  allowance always wins; a qualifying budget is then reported in the tile's
  note rather than silently replacing it.

### Changed

- **Settings: the GitHub Copilot tab is now a Microsoft tab** with Azure,
  Foundry and Copilot sub-headings. Copilot's controls moved unchanged.
- **Copy diagnostics redacts Azure identifiers** alongside email addresses.
  Subscription and tenant GUIDs identify the account the way an email address
  does, and resource-group and resource names are chosen by the account holder
  and routinely name a client or a project. The resource *shape* is preserved,
  so a pasted blob still says which provider and resource type was involved.

### Notes

- **Cost Management is gross of credits.** It reports consumption and excludes
  free and prepaid credit; there is no credit line to subtract. The gauge
  therefore measures spend against an allowance *you* state and is not a live
  read of a Visual Studio or MCA credit balance. Said on the tile, in the
  settings hint, and in the README.
- **The data lags.** 8–24 hours on EA/MCA, up to 72 on pay-as-you-go,
  refreshed about six times a day. Every snapshot carries the latest usage date
  it actually saw, so the tile reports how old the number is instead of
  implying it is live.
- **The tile fetches at most once an hour.** Cost Management quotas are shared
  tenant-wide, and the refresh loop above this can fire every five minutes when
  active and every minute during the error fast-retry. Between fetches the
  cached result is served with its original timestamp. Microsoft's own guidance
  is no more than once per day.
- **Azure Sponsorship offers get a warning instead of a gauge.** Cost
  Management does not support them: it reports zero while the sponsorship
  credit drains, and a reassuring 0% gauge is a lie the user has no way to
  detect. Detected from `subscriptionPolicies.quotaId`.
- **The tile never assumes a currency.** Amounts are shown in whatever the
  API's `Currency` column reports, with the code printed alongside.

### Hardening from review

Three reviews — security, correctness and adversarial — went over the Azure
work in three rounds before it merged, each round re-reading the previous
round's fixes. What follows is not a diary of the rounds: it is the set of
rules that came out of them and now stand, with the failure each one closes.

- **The hourly throttle fails closed.** It is the property the whole module is
  shaped around, because Cost Management quotas are shared tenant-wide. Inside
  the window with nothing cached and nothing remembered, the gate returns a
  "waiting for the next fetch window" snapshot rather than falling through to
  a live fetch — the old gate only engaged on state a *finished* fetch leaves
  behind, so one exception outside `_fetch`'s except list left `_State` blank
  and the tile fetched on every refresh cycle. Every outcome is now recorded,
  including an exception no one anticipated; one malformed field was enough to
  reach the old hole (a non-string `nextLink`, or a `Retry-After` of `inf`,
  which threw the 429 handling away along with the server's own back-off).
  A fetch already in flight dispatches nothing more, a dispatch that fails
  before the worker runs does not leak the in-flight flag, the flag expires
  after 15 minutes so a lost worker cannot park the tile for the life of the
  process, and a clock that jumped in either direction cannot park it either.
- **Two identities, because a settings save is not a new tenant.** The
  credential triple (tenant, client, secret digest) is one identity: changing
  it resets the whole `_State` and the cached bearer, since everything held
  describes a different app registration. What the request *asks for* — the
  reset day, the resource group, the Marketplace toggle, the pinned Foundry
  ids — is a second: changing it drops the gauge but not the throttle, because
  "at most one live fetch an hour" is a promise to the tenant rather than to
  this tile, and ten OK presses in Settings used to be ten live fetches. The
  old figures are still shown, with no percentage on any row and a note saying
  the settings changed and when the next fetch is due. The row count and the
  allowance are display settings and are in neither identity: both re-render
  from the cached aggregate with no API call, which is why the aggregate keeps
  every distinct bucket rather than only the rows the setting asked for.
- **The cost query follows `nextLink`.** The Query API pages at ~1000 rows,
  which Daily granularity grouped on ResourceId and ServiceName reaches at
  roughly 33 resources — so page 1's subtotal was being shown as the month's
  spend on exactly the busiest subscriptions. Pages are followed back to ARM
  only, capped at 20, and every way the loop can end early marks the result
  truncated: a refused host, a non-200, a page whose body carries no readable
  cost column (an ARM error document returned as 200 on page 7 merged as zero
  and gauged the under-reported total), a link back to a page already read,
  and a spent request budget. A repeated link is the one case where the
  subtotal is too *high* rather than too low, and the note says so instead of
  "read so far". One refresh also has a wall-clock deadline and a request
  ceiling for its page loops.
- **The tile never prints a number it has just disowned.** One flag governs
  the summary percentage, the breakdown shares and whether there is a forecast
  row at all: an allowance, a complete read, one billing currency, a readable
  offer type, no Sponsorship offer, and settings that have not changed since
  the figures were read. A share of a total the summary refused to gauge is
  the same wrong number one row down, and a forecast built from it is that
  number projected. The amounts are always shown; only the percentages go.
  More than one billing currency shows per-currency subtotals rather than
  their meaningless sum, and a truncated read reads "incomplete" on the row
  with the subtotal moved into the note.
- **An unreadable offer type shows no gauge, whatever the total.** Cost
  Management Reader without Reader is the likely role split, and an Azure
  Sponsorship subscription reports zero cost while the sponsored credit
  drains. A positive total does not rule that out — a sponsored subscription
  still bills Marketplace and other non-sponsored charges normally — so
  **Reader is required for a gauge**, not only for the Foundry roll-up. Said
  in the README and in the settings hint.
- **A typed allowance is always the denominator.** "Smallest qualifying budget
  wins" is the right rule between budgets and the wrong rule against a number
  a person typed: a 1.00 alert canary or a per-team budget would otherwise
  take the tile and the tray dot over. A qualifying budget is reported in the
  note instead, and becomes the denominator only when nothing is typed. Even
  then it has to measure the same money: a budget in another currency, one
  scoped to a different resource group, one covering the whole subscription
  while the tile is filtered to a resource group (it covers spend the tile
  does not measure, so the gauge would under-report), a non-finite amount, or
  any budget at all when the reset day is not the 1st (a Budget's monthly
  grain is the calendar month) is refused, with a note saying so.
- **The Marketplace row moves a charge, not a resource.** It was keyed on
  resource id, so a resource with 900.00 of ordinary usage and 1.00 of
  Marketplace charge reported all 901.00 as Marketplace spend; it is keyed on
  the (resource, service) pair with a per-row clamp. A Marketplace query that
  was cut short discards the split for that refresh rather than moving part of
  a resource's charge and leaving the rest — the month's total does not depend
  on it, so the row goes and the note says why.
- **A tolerant sub-fetch costs a row, never the tile — and never the
  back-off.** The offer read, Foundry discovery, budgets, the forecast and the
  Marketplace query all share one shape: re-raise a 429, because it is an
  answer about the whole tenant and dropping it meant issuing more requests
  inside the window Azure had just asked us to stay out of; note a permission
  error; note anything else. A 5xx raises rather than reading as an empty
  answer, so the note fires for the failure that actually happens rather than
  only for the rarer 403.
- **Identifiers do not ride out on an error string.** A transport failure
  stringifies with the request URL, and every ARM URL carries the subscription
  or tenant GUID; Copy diagnostics redacted it but `ai-gauge.log`, the tile
  tooltip, the error dialog and `--probe` did not. Errors name the exception
  type instead of quoting it, no Azure log line writes a traceback, and the
  dialog header redacts first and escapes last so the markers are visible
  rather than eaten as unknown tags. The redaction pass is a scalpel: child
  resource names and `%2F`-encoded paths are covered, names containing an
  apostrophe redact whole, and the un-hyphenated GUID rule is anchored to
  Azure contexts so another provider's md5 or session id survives the blob.
  `_sanitize_raw` caps dict keys, list, tuple and set contents and depth; the
  email pattern is bounded at both ends and linear, with bounds wide enough to
  cover the whole run they have to match; and the AAD error code is one token.
- **The cached bearer token behaves like the credential it is.** Keyed on the
  secret's digest, dropped before *and* after the secret is saved or cleared,
  dropped on an ARM 401, and dropped when the app registration changes — a
  rotated secret used to keep producing a working tile for the rest of the
  token's lifetime.
- **Nothing the server sends can steer a request.** `nextLink` is host-pinned
  on both page loops; `Retry-After` is parsed defensively and capped; the cost
  column is found by name, never by position, and a name that makes it an
  identifier or a classification (`CostCenter`) is refused; row counts,
  currency codes and service names are bounded at parse time; a usage date in
  the future is not "data as of". A poisoned `config.json` coerces field by
  field instead of failing to load, and a resource id containing a path
  traversal segment is refused.
- **The allowance period is a UTC window**, matching how Cost Management dates
  its usage, with the boundary shown in local time — the printed reset day now
  comes from the same instant the countdown counts to. The summary row's label
  is stable ("Spend this month"), with the amounts alongside the bar, because
  history keys an in-flight period on the label and a label carrying the
  running total opened a new period on every fetch.
- **Refresh cadence ignores `reset_label`, for every provider.** It is a
  caption — a ticking countdown, or spend to the cent — and either one counted
  as activity and pushed the whole app back into active-cadence polling.

### Testing

- 907 → 1099 tests. Every finding in all three rounds has a regression test
  that fails on the code as reviewed — round 1 shipped five that did not,
  which a mutation run over the fixes is what found, and each later round's
  fixes were mutated the same way. Several drive the real worker rather than
  the inline stand-in the suite had been using (which is why the throttle hole
  was invisible to it), and the UTC-period tests run under a non-UTC zone,
  where they had been tautologies.

## 1.1.0+cfa.3 - 2026-09-09

Claude and Codex tiles read **every meter their pages show**, not two, and the
label definitions moved out of the extractor source into a catalog the app can
extend by itself. Claude changed its usage surface three times in one week
before this release; a renamed row is now a one-line edit to a JSON file
instead of a code change.

### Added

- **One field per meter.** Claude reports Session, Weekly, Opus only, Sonnet
  only, Cowork only, Claude Design and Daily routine runs; Codex reports its
  5-hour and weekly cards plus any other usage card the page renders. Session
  and Weekly stay untagged and still drive the tray / menu-bar colour on their
  own; every other meter is informational, shown when the tile is expanded.
  Opus at 91% of a sub-limit is not the account being at 91% of its quota.
- **A meter catalog, in data.** `src/aigauge/providers/meter_catalog/{claude,codex}.json`
  ships with the app and is overlaid by `<app data>/meter_catalog/<kind>.json`.
  Each entry carries a canonical key, a stable display label, the page wordings
  (`aliases`), its window and whether it is primary. The bundled catalog
  reproduces exactly the labels the extractors carried before it. Format
  documented in README.md.
- **A weekly self-scan.** On the first refresh after seven days — or right away
  via **Settings → General → Re-scan meters now** — each provider is asked for
  every labelled row its usage container renders, and rows the catalog does not
  recognise are adopted as informational meters. They appear as fields on the
  next refresh, and `"enabled": false` in the override file switches one off
  **and keeps it off**: a disabled or parked entry is still a label the catalog
  knows, so the next scan does not adopt it again under a new key.
  Adoption requires a percentage, used/remaining wording beside it (a row the
  reader can never read is not a meter), a short alphabetic label that matches
  no page furniture, and either reset wording or a position inside the usage
  container. A label that is a meter the catalog already has is refused: the
  same wording, a whole-word *fragment* of a known label ("Current" inside
  "Current session"), or a known label with a count glued on and no new word of
  its own ("Daily included routine runs 3 of 10"). A candidate that adds a word
  of its own is a new meter — "Weekly Opus" and "Cowork session" are the shapes
  Claude actually ships — and the collision that corrupts something, a row
  carrying an existing meter's *display* label and therefore its history key,
  is refused outright. The 200-meter read cap does not narrow what adoption
  knows: an entry past it is still in the file, so it cannot be adopted a
  second time.
  An adopted meter is given no window for the same reason it is given no
  polarity: a period guessed from the wording can make an active meter read
  "idle". Adoption is capped at 24 meters per provider and never sets
  `primary`. The scan is local — it reads what the embedded browser already
  rendered, downloads nothing, and there is no remote catalog.
- **The scan only counts when the page could actually be read.** Adoption and
  the weekly stamp happen after the snapshot is classified, only on an OK
  snapshot, only when the extractor found a usage container, and once per
  refresh. A logged-out, challenged or half-rendered page would otherwise adopt
  whatever it happened to render — permanently — and spend the week's scan on a
  page nobody could read. The extractor distinguishes "the panel showed nothing
  new" from "there was no panel to look in"; the second leaves the scan due and
  says so in the log.
- **Provenance on every adopted meter.** An adopted entry is structurally
  identical to a bundled one, so each records `source`, `first_seen`, the
  `account_id` its page belonged to, and the `evidence` that justified it (the
  row's label, percentage and reset wording, capped at 200 characters with any
  email redacted — the same treatment the diagnostics blob gets). Bundled
  entries declare `source: "bundled"`; an entry added by hand with no `source`
  becomes `"user"`, because only the packaged file is bundled. Unrecognised
  fields in an override file are preserved rather than dropped, and an entry
  can carry a `status` other than `"active"` to park it — the hook a later
  review step needs. A discovered meter whose wording a later bundled meter
  covers is dropped at load with one `superseded` line, rather than reporting
  the same number twice for ever.

### Fixed

- **Codex read a bare percentage as "used".** `readCard` tested the whole card
  for "used"/"remaining", so a card rendering only "42%" resolved to 42%
  consumed — and if it meant 42% *left*, the gauge pointed the wrong way. Worse,
  the plain-text fallback reads a window that can run into the next card, so one
  card's wording could set another's polarity. Polarity is now read beside the
  percentage exactly as Claude's `readRow` does, stopping before any countdown
  so "2 hr left" cannot invert the gauge, and a card with no wording beside its
  number is refused rather than guessed. `_parse_body_card` carried the same
  defect and got the same rule. This was the first entry in
  `docs/next-session.md`'s known-defect list.

- **A collapsed container gives Session no number rather than the wrong one.**
  `readRowText` takes the last percentage in the element it picked unless a
  rival label is in there too, so with adopted meters left out of the rival set
  an adopted row sharing one container with Session made Session report *that*
  meter's percentage — as an OK snapshot, on every refresh. The rival set is
  every meter the page can render: bundled, discovered and hand-added, enabled
  or not. Fragments like "Current" and "Opus" are refused at adoption instead.
- **A usage panel the app cannot find is diagnosed daily, not every refresh.**
  A page rendering fewer than two percentages never completes a scan, and the
  scan was left due — so `discovery_no_container` was logged every time the
  tile refreshed, for as long as the page kept that shape. The scan is stamped
  to come due again in a day. A payload that never reached the scan at all
  still leaves it due.
- **Switching both primary meters off is a setting, not an error.** Claude's
  new "a primary must have been read" guard fired on a catalog with Session and
  Weekly disabled, so the tile reported a layout change about the user's own
  override file, permanently. Codex already tolerated it.
- **An unreadable informational row is named once per refresh.** Adoption
  rebuilds the snapshot, and the rebuild logged the same line again — two lines
  read as two meters gone rather than one.
- **A row labelled "Constructor" is no longer dropped.** Both extractors kept
  their per-key maps in plain objects, so every name on `Object.prototype` was
  already present: that row vanished from every scan and a catalog key of
  `constructor` was never read.

### Changed

- **A Codex card with no readable percentage is no longer shown as a blank
  gauge.** It counts as absent, which makes the page a partial render and earns
  a retry, then a plain error. This matches Claude's rule for the same case.
- **The extractors walk the DOM once per run**, not once per label. The catalog
  turned a two-label read into a dozen, and `querySelectorAll` plus `innerText`
  over every element is the expensive part of both extractors.
- `meter_catalog_last_scan` is a new config key (per provider kind). Missing
  counts as due, so the first refresh after upgrading scans, and so does a
  stamp that cannot be read. A stamp in the *future* — a DST rollback, a
  corrected system clock — is clamped to now rather than treated as due, which
  had meant scanning on every refresh until the clock caught up. A
  timezone-aware stamp is converted to local time instead of raising
  `TypeError` out of `refresh()` and erroring the tile permanently.
- **The adaptive refresh cadence follows the primary meters only.** Its
  signature hashed every metric, so with a dozen informational rows per
  provider any one of them twitching reset the backoff that exists to stop
  polling a provider that is not moving.
- **Claude requires that a primary meter was actually read.** A payload of
  informational rows only used to build an OK snapshot with no gauge on the
  tile and nothing to retry. Codex has refused that since its partial-render
  bug.

### Security

- **The override file is written atomically** (temp file plus `os.replace`),
  and `0600` where POSIX modes exist. It carries page-derived labels, capped
  evidence and the account id a meter was seen on. On Windows there is no
  POSIX mode: it relies on the user-scoped `%APPDATA%` location like
  `config.json` beside it, which SECURITY.md now says rather than claiming
  owner-only everywhere.
- **Nothing is written over a file the app could not read or could not move
  aside.** A file that does not *parse* is moved to `<kind>.json.corrupt`
  before the write — one trailing comma in a hand edit used to cost every other
  edit in the file — and if that move fails, the write is abandoned rather than
  destroying what the move was meant to keep. A file that could not be *read*
  (a Windows sharing violation, a permission change) is left exactly where it
  is: a failed read says nothing about the contents, and treating it as
  unparsable had a valid file quarantined and replaced. A second corruption
  keeps the first quarantine and discards the newer copy, loudly.
- **The usage container cannot leave the usage panel.** Discovery is confined
  to the element holding the panel, and that element was chosen as the smallest
  one carrying a marker phrase and two percentages — which is `<body>` whenever
  the phrase also appears outside the panel, as it does in Claude's settings
  nav and in Codex's prose above the cards. Page furniture ("Storage 88%",
  "Save 20%") was then inside the usage container. The panel is found by
  walking up from a marker that carries a percentage of its own — a bare
  mention is a nav item, not a panel — and among the candidates that survive,
  the *richest* wins: the one holding the most of the catalog's meters and the
  most rows whose percentage says used or remaining. Size was the old
  tiebreak, and furniture that carries the marker and a number of its own
  ("Plan usage 12% off Max" beside "Storage 88% used", a sidebar of chat
  titles) is always shorter than the panel, so it won and the panel was never
  scanned. A candidate holding a bare marker off the path from its anchor is
  refused as well, once the climb has passed something panel-shaped: that is
  how a panel with a single meter promoted the SPA's root wrapper, which is
  not `<body>` and so escaped the outright refusal — while a panel's own
  heading, tab strip or footnote is a bare marker *inside* the panel, and
  refusing on those refused the panel itself. `<body>` itself is still
  refused, and so is a container several times longer than the rows it holds —
  counting the rows themselves, since counting every wrapper around them
  multiplied the total by the nesting depth and let a page-swallowing element
  through. Only one container is returned, so a page rendering two usage
  panels has the richer one scanned and the other one's meters left
  undiscovered.
- **The extractor's injection markers are filled in one pass.** Three chained
  replaces rescanned inserted text, so a label reading `__AG_CATALOG__` spliced
  JSON into a string literal and the whole extractor stopped parsing.
- **A meter past the read cap is still a rival.** The 200-meter cap is a budget
  for DOM walks, and naming a meter in the extractor's rival set costs no walk:
  built from the capped catalog, an entry past the cap was a meter the page
  still renders and the extractor no longer knew about, so one sharing a
  collapsed container with Session could hand Session its percentage. The rival
  set is built from the whole file, the way adoption already reads it.
- **`kind` is validated inside both path builders** — shape, and the Windows
  device names (`con`, `nul`, `com1`…) `config` already refuses for profile ids,
  because `nul.json` is a device and not a file. A catalog is capped at 200
  meters for reading. `secret_storage._atomic_write` now applies a requested
  mode without `os.fchmod` where there is none, so a Windows caller asking for
  one gets a file rather than an `AttributeError`.

### Packaging

- The catalog is data next to the code, so both packaging paths name it
  explicitly: `artifacts` in the hatch wheel config and `--add-data` in
  `build.sh` / `build.ps1`. A frozen build that silently dropped it would read
  no meters at all, and only on a user's machine.

### Testing

- 610 → 827 tests. Catalog loading, override merge, load order and alias
  matching; the seven-day gate including the clock-change, future-stamp and
  timezone cases; adoption, its idempotence, its cap and every junk rule,
  including that a disabled meter is not adopted again and that a display-label
  collision cannot fabricate a history rollover; provenance including the email
  redaction and the preservation of unknown fields; the catalog scan and the
  discovery scan executed in node against a stub DOM whose containers derive
  their text from their children, covering the settings-nav layout, the
  workspace-credit layout and nested wrappers; `node --check` on an extractor
  built from a catalog whose label looks like an injection marker; Codex
  polarity for used, remaining and a bare percentage; and that a page relabel
  keeps one history key rather than forking it. Plus, for each way out of the
  usage panel, a stub DOM that takes it: a nav carrying the marker and its own
  percentages, a nav carrying the marker and a percentage too, an SPA root
  wrapper reached from a single-meter panel, and rows buried six wrappers deep
  inside a page-swallowing element. Plus the panel's own heading, tab strip and
  footnote, which name the meters without measuring them and used to refuse the
  panel that renders them. And the collapsed
  container in both orders, which is where a rival label is the difference
  between Session refusing and Session reporting another meter's number.

## 1.0.0+cfa.2 - 2026-08-10

**`1.0.0+cfa.1` does not start.** It raises `AttributeError` during `App.__init__`
before its window appears. If you are on it, upgrade; there is no workaround
short of rolling back.

This release exists because of that, and the version number is the point: a
build that crashes on startup and a build that does not must not report the
same string. `app_version` appears in every diagnostics blob, and that is the
whole reason `+cfa.N` exists.

### Fixed

- **Startup crash.** `_consecutive_error_cycles` was read by `_error_retry_time`,
  which `__init__` reaches through `_restart_timer`, but the assignment never
  landed in `__init__`. Introduced by the refresh change below, in the same
  release.
- **A failed provider is retried within the minute rather than the interval.**
  The recovery path required the errored snapshot to still carry stale metrics,
  so the *worse* case waited longer: a provider that had never succeeded this
  run, showing nothing at all, waited a full refresh interval, while one showing
  a stale-but-plausible number was retried within the minute. That is the shape
  of a cold start — Claude's settings page resolves eight endpoints before it
  requests usage, and on a fresh profile none are cached — so every restart
  showed a broken tile for five minutes before silently fixing itself. Any error
  now earns the fast retry, bounded at three consecutive failing cycles.
  `AUTH_REQUIRED` is excluded: signing in is the user's move.

### Testing

- **Nothing constructed a real `App`.** Every test used `App.__new__(App)` with
  hand-set attributes, so an attribute read at startup but never assigned in
  `__init__` was invisible to the suite — and the stand-in stubs *had* the
  missing attribute, so they proved the logic and hid the wiring. A smoke test
  now builds the real object; it fails for any attribute the startup path reads
  and `__init__` does not provide.

## 1.0.0+cfa.1 - 2026-08-10

**First release numbered independently of upstream.** The `+cfa.N` segment
already marked these builds as this fork's, but `0.6.5` sat inside upstream's
own `0.6.x` range, so the suffix was the only thing telling them apart — and a
suffix is exactly what gets dropped in a filename, a bug report or a
conversation. `1.0.0` makes the divergence structural instead of something a
reader has to notice. The number before `+` is this fork's counter and always
has been; it is not a claim about which upstream release the tree matches.

The release itself is the Claude work below. Claude stopped reporting entirely
after it moved its usage surface, and
work to fix it exposed several defects in the machinery meant to make such
breakages diagnosable. Nothing here changes what the gauges mean; it changes
whether they can be trusted and whether a failure can be explained.

### Fixed

- **Claude reads again.** Claude moved usage from a dialog opened by the
  `#settings/usage` hash to a real page at `/settings/usage`, and stopped
  opening the dialog from the hash at all. The app kept loading the old URL and
  waiting for rows that were never coming, failing on every refresh. It now
  loads the real page. The recovery path that should have caught this could
  not: it asked whether the URL *looked like* a usage route, and since the app
  navigates to a usage URL itself, that was true from the first poll. Route
  decisions are now made on what the page actually renders.
- **The settings page is given time to load.** It resolves eight other
  endpoints — org, feature, memory, MCP, marketplace, i18n — before it requests
  usage. The old budget gave up after roughly 13 seconds, while the page still
  read "Loading...". Claude now polls sooner and for far longer. Other
  providers are unchanged.
- **Codex no longer reports an untouched weekly quota as a broken page.** An
  idle account showing 100% remaining was rejected as a half-rendered layout,
  so the tile errored permanently. OpenAI's current wording for the shared
  limit is also recognised; only the older phrasing was.
- **Claude's page text no longer includes its stylesheets.** Text was read in a
  way that concatenated the source of inline `<style>` elements. Because CSS is
  full of `width:100%`, two checks that depend on the *absence* of a percent
  sign could never fire.
- **Sign-in verification checks the session, not the usage panel.** It asked
  whether the usage dialog had rendered, so a change to that panel made a
  perfectly valid session unverifiable and "I'm signed in" could never succeed.
- **Failed scrapes say why.** Chromium reports the failure reason *after* it
  reports that the load finished, so every load failure was recorded with an
  empty error code and an empty error string — the diagnostics emptying
  themselves at the only moment they matter. Snapshots now carry the real
  reason, e.g. `net::ERR_CONNECTION_RESET`.
- **The menu-bar dot ignores breakdown rows.** OpenRouter's per-model rows
  carry each model's share of spend, so one model dominating spend drove the
  dot red with no quota near its cap. The tray was fixed previously; the macOS
  menu bar is a second implementation and was missed.
- **A single bad number in `config.json` no longer discards the block around
  it.** Bounded settings are clamped to their range instead of rejected, so one
  out-of-range value costs only that value.

### Added

- **A reading that cannot be justified is refused rather than guessed.** Two
  silent failures were possible: a percentage that could not be attributed to
  one meter when several share a container, and a percentage with no
  "used"/"remaining" wording beside it, which was assumed to mean *used* and so
  displayed "42% left" as 42% consumed. Both produced a plausible wrong number.
  The tile now reports which row it could not read and why, and carries that
  row's text for diagnosis.
- **Diagnostics record the shape of the JSON the Claude page fetches**, so a
  future change can be diagnosed from a real account instead of inferred from
  rendered wording. No response body is ever kept: numbers, booleans and
  timestamps survive, every other string becomes a length marker, and the
  reduction happens inside the page. Local only — see "API response shapes" in
  `SECURITY.md`.

### Security

- **Page scripts could poison the local log and the diagnostics clipboard.**
  The API capture was exposed as an ordinary property in the page's main world,
  which everything the provider page loads can write to. A script replacing it
  with 50,000 keys produced a 1 MB log line against a 512 KiB rotation —
  destroying the existing diagnostics — and put content of its choosing into
  the blob users are asked to paste into bug reports. The capture now lives
  behind a property that cannot be reassigned, and both the log and the
  clipboard bound how much of a provider payload they will reproduce.

### Documentation

- The **PowerShell 7 requirement for `build.ps1`** is documented in the README,
  `CONTRIBUTING.md` and `RELEASING.md`. The script declares
  `#requires -version 7` and refuses to run under Windows PowerShell 5.1 — still
  the default `powershell.exe` — reporting a `#requires` error rather than a
  build failure. This was stated nowhere and cost a build cycle. A test reads
  the requirement from `build.ps1` so the docs cannot drift from it.
- `SECURITY.md` describes what the API-shape capture records and what it
  discards, and states that scraping rendered text is the fragile layer and
  that unjustifiable readings are refused rather than shown.

## 0.6.5+cfa.1 - 2026-07-31

First release under the fork's explicit versioning scheme, plus a substantial
documentation pass making the fork's identity and divergence from upstream
explicit.

### Added

- Per-account **gauge colors**: each Claude/Codex/OpenCode account, plus Copilot and OpenRouter, can set its own three severity cutoffs and four band colors from Settings ("Colors" on an account row, "Gauge colors…" on a provider tab). Defaults reproduce the previous appearance.

### Changed

- **The tray and menu-bar indicator now use the same severity cutoffs as the gauges.** They previously had their own fixed 75%/90% thresholds while the bars used 60%/80%/95%, so the indicator turned amber at 75% and red at 90%. It now follows the shared (and configurable) bands, meaning **red starts at 95% instead of 90%** by default. Lower the red cutoff for an account if you want the earlier warning back.
- Compact summary chips derive their fill by darkening the band color instead of using fixed tones, so custom colors keep the bright-bar / dark-chip pairing. The default chip tones shift very slightly.
- The tray indicator no longer counts OpenRouter's model-breakdown rows. Those carry each model's *share of spend*, so one model at 96% of spend drove the indicator red with no usage limit anywhere near its cap.
- **Versioning:** fork releases now carry a PEP 440 local segment (`0.6.5+cfa.1`). The number before `+` is this fork's own counter, not a claim about which upstream release the tree matches. `tools/check_versions.py` now rejects a bare number in CI. Release archives substitute `-` for `+` in the filename, because GitHub normalises some characters in release asset names.
- **macOS release artifacts are built again.** They were dropped when the release matrix was narrowed to Windows + Linux, while the macOS code and test matrix stayed — so the app kept working on macOS but stopped shipping a binary. The `.app` bundle is back, with provenance attestation like the other two. It is **not** signed or notarized: Gatekeeper reports an unsigned downloaded bundle as *"damaged and can't be opened"*, which means unsigned, not corrupt. Clear it with `xattr -dr com.apple.quarantine`, or run from source. Documented in the README and in the release body.
- **Release workflow no longer interpolates the git tag into shell commands.** A tag is attacker-supplied text and git ref names permit `;`, `$`, `&` and backticks, so `${{ }}` inside a `run:` block executed them — a crafted tag ran commands before the version check could reject it. Values now arrive via `env:` as quoted shell variables, and the tag is pattern-checked against the fork scheme before any other step sees it. Reaching this required tag-push access, so it was maintainer-only, but it is a real hole and the fix is cheap.
- **Documentation** now describes the fork rather than upstream: the README's CI badge and download links pointed at upstream, so the front page was directing people to the *unhardened* upstream binaries. Security reports, issue-template links, contributing instructions, release process and project URLs are all routed to this fork; a new "Relationship to upstream" section records what diverges and why.

### Fixed

- `Config.load()` no longer discards the entire configuration when any single setting fails to parse or validate. A bad `window.height`, an out-of-range `opacity`, a wrong-typed `expanded_tiles`, or a file truncated by an interrupted write each used to cost named accounts, Copilot username/quota/billing org, OpenRouter budget, OpenCode workspace URL, window geometry, autostart and provider toggles — made permanent by the next save. Valid settings are now salvaged key by key, and a file that can no longer be honoured is preserved as `config.json.corrupt` instead of being silently destroyed.

## 0.6.4 - 2026-07-28 — security hardening

> Fork release. **Not** upstream's `v0.6.4`, which is unrelated code; this was
> built from upstream `v0.6.3` (`1df4536`).

Security-hardening release following a full source audit (see
`SECURITY-AUDIT.md`). The audit found the source clean; these are hardening
fixes, each with regression tests. Release builds are Windows + Linux only and
carry a signed provenance attestation (verify with `gh attestation verify`).

### Security

- Secret storage: require an explicit opt-in to read a non-Windows plaintext `secrets.dat`, write it `0600`, write atomically (temp + `os.replace`), quarantine (not overwrite) an undecryptable file, log DPAPI failures, pass `CRYPTPROTECT_UI_FORBIDDEN`, and apply an explicit owner-only Windows DACL.
- Embedded sign-in browser: block `data:` top-frame navigation (anti-phishing).
- Account removal now deletes the account's whole embedded-browser profile (live cookies + cache), not just the stored cookie blob; added a **Clear all browser data** button.
- OpenCode: inject only allowlisted cookies from a pasted header (drop foreign/tracking cookies); validate the workspace usage URL (`https` on `opencode.ai` only — no `file:`/`data:`/other host/port/credentials) before any load. OpenCode's session cookie stays script-readable so the page still hydrates.
- Account/profile ids are validated against path traversal (and Windows reserved device names) before use as filesystem paths. A single invalid field in a saved config now falls back per-field on load instead of resetting the whole config.
- "Copy diagnostics" now redacts email addresses and truncates scraped page text.
- CI: pin all GitHub Actions to commit SHAs, drop `GITHUB_TOKEN` to least privilege, add build-provenance attestation, and pin the build-time PyInstaller version.

## 0.6.3 - 2026-07-10

### Added

- Added **OpenCode** usage tracking with Rolling, Weekly, and Monthly meters, a configurable workspace Go usage URL, Settings controls, and provider tile support in both expanded and compact views.
- Added OpenCode cookie-paste setup for accounts that cannot complete Google sign-in inside the embedded browser.

### Changed

- Claude, Codex, and OpenCode provider rows can collapse to compact inline metric chips, while the session-to-weekly burn-rate label remains available again when the row is re-expanded.
- OpenCode Rolling usage now uses the same 5-hour pacing window as the provider, so the elapsed-time tick marker appears on the Rolling bar.

### Fixed

- OpenCode cookie injection now targets the `opencode.ai` profile correctly, accepts full `Cookie:` headers copied from DevTools, and avoids forcing HttpOnly on OpenCode cookies so the signed-in page can hydrate normally.
- OpenCode refreshes requested while another provider is already in-flight are queued instead of leaving the tile stuck at `loading...`.
- The OpenCode sign-in dialog now explains the Google embedded-browser block and points users to the paste-cookie fallback.
## 0.6.2 - 2026-06-25

### Added

- Added a **Fade when inactive** setting. When enabled, the floating widget fades while it is unfocused and the mouse is away, then returns to full opacity when hovered, focused, or dragged.

### Changed

- **Fade when inactive** now defaults to 80% opacity, keeping the widget easier to read while still getting it out of the way.

### Fixed

- Expanding or collapsing the OpenRouter model breakdown now immediately refits the floating widget height, so the model rows appear or disappear without needing to drag or move the window first.

## 0.6.1 - 2026-06-20

### Fixed

- The widget no longer vanishes at high Windows display scales (175%/200%) while the tray icon and Settings stay reachable. The last position is saved in device-independent pixels; raising the OS scale shrinks the logical desktop, so a spot that was on-screen at 100–150% could fall entirely outside the visible area — leaving the app running but the widget parked off-screen. The saved position is now clamped onto the nearest visible screen both when it is restored and on every show, so a scale or monitor change can never strand the widget out of view.

## 0.6.0 - 2026-06-19

### Added

- A **UI scale** setting (Settings → General) resizes the whole widget from 75% up to 400% — enlarging it for high-resolution (4K) displays where the otherwise fixed-pixel layout could render very small, or shrinking it for a more compact footprint. It is applied through Qt's display scaling (`QT_SCALE_FACTOR`) so fonts, bars, and icons stay crisp; changing it offers to restart AI Gauge so the new size takes effect immediately.

### Changed

- Windows Start at login now creates a named Task Scheduler entry instead of writing an `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value, reducing Defender false-positive risk from Run-key persistence.
- Windows PyInstaller builds now include generated executable version metadata for product, company, description, filename, and version fields.

### Fixed

- **Start at login** on Windows now actually registers, and saving Settings with it enabled no longer crashes the app. The Task Scheduler entry had two bugs: it was written as UTF-8 (rejected by `schtasks /XML` as malformed — "unable to switch the encoding"; now UTF-16), and its logon trigger/principal had no user scope, so Windows treated it as an all-users task and refused to register it without admin ("Access is denied"). It is now scoped to the current user via `UserId`, so no elevation is needed. Settings are also persisted *before* autostart is wired up, and any remaining autostart failure surfaces a warning instead of aborting the app.
- Unhandled exceptions are now written to the log via an excepthook, so a crash in a Qt slot leaves a diagnosable trace instead of silently terminating a windowed build.
- The app no longer quits when a dialog or message box is the last window dismissed: `setQuitOnLastWindowClosed(False)` is now applied to the live `QApplication` instead of (ineffectively) before it was constructed, so the tray-resident app stays running.
- Settings dropdown and spin-box arrows now render as proper chevrons instead of empty grey blocks.
- The sign-in window's **I'm signed in** check no longer hangs and falsely reports `Could not load verification page (timeout)` right after a successful sign-in. Because the embedded browser was already on `claude.ai/new`, navigating it to the `…/new#settings/usage` verification URL was a same-document (fragment-only) change that never emits a load-finished event, so verification waited out its full timeout. It now polls for the signed-in marker on a timer instead of depending on that event, while still fast-failing a genuine load error.

## 0.5.9 - 2026-06-01

### Changed

- Claude usage scraping now targets Claude's app-shell usage dialog route and no longer carries the retired separate design-generation limit through settings, config, or tile rendering.

### Fixed

- The session-to-weekly burn rate now treats a mid-week weekly reset (Claude occasionally zeroes the weekly counter while keeping the same reset date) as the same week rather than a new one. Previously any weekly percent drop was read as a rollover, which could record a spurious partial week and restart the current week's estimate. Now only a forward jump of the weekly reset date starts a new week; a same-date drop is skipped as a discontinuity while the week's accumulation is kept, so the estimate just drifts toward the new ratio that week and locks onto it the following week.
- Session-to-weekly history now normalizes old split records that were finalized for the same weekly reset, so stale fragments like a 1% coverage tail no longer appear as fake prior weeks. The history dialog now labels rows by their actual observed dates and shows `n/a` for low-confidence ratio values instead of displaying noisy estimates.
- Claude refreshes now wait for the Session and Weekly usage rows specifically, so unrelated percentage text in the shell no longer causes a premature stale error while the usage dialog is still hydrating.
- Stale error snapshots now pull the next automatic refresh forward to a short recovery retry instead of drifting into the idle backoff cadence.

## 0.5.8 - 2026-06-01

### Added

- Claude and Codex tiles now show a session-to-weekly burn rate on the right of the tile header (e.g. `~9.2/wk`), meaning how many full sessions you can run before the weekly limit is used up. It is measured empirically from the readings AI Gauge already collects (no extra scraping): while a session is counting, weekly usage climbs in proportion, so the ratio of those increments gives a stable estimate. Hovering shows the percent-of-weekly-per-session framing plus a recent-weeks trend, and clicking opens a small history dialog with a sparkline and the last 26 weekly ratios so you can see how the providers retune their limits over time. The dialog also shows two at-a-glance views of the current ratio: how much each full session costs in weekly percent, and how many full sessions remain in the current week (based on the live weekly percent used), plus a `Typical (last N weeks)` median once a few weeks are recorded. The header value is the usage-weighted average across all sessions in the current weekly period and keeps refining as the week goes; at a new week it carries over last week's value (dimmed, with a `°` marker and the new week's calibration progress in the tooltip) until the new week has enough data to stand on its own. Idle/unused windows are ignored, and readings outside 2-99% (start-of-window floors such as Codex's idle `1%`, and saturated tails) are excluded so neither end can skew the estimate. The value reads `burn ~?` while calibrating until there is enough usage for a stable reading.

### Changed

- GitHub Copilot now reads the current billing usage summary endpoint and displays monthly AI credits instead of legacy premium requests. Plan defaults were updated for the June 1, 2026 credit model (Pro 1,500, Pro+ 7,000, Max 20,000), and request-based accounts still fall back to GitHub's legacy premium-request endpoint.

### Fixed

- Fixed Copilot credit parsing for GitHub's live `copilot_ai_unit` / `ai-units` billing SKU, so Pro accounts now show returned credit usage such as `68.6/1500` instead of `0/1500`.

## 0.5.7 - 2026-05-18

### Added

- Claude and Codex settings tabs now include an `Open usage in browser` button so you can jump straight to each provider's web usage page from the app.

### Fixed

- Claude's in-page usage-panel polling timeout is now treated as a retryable scrape failure, so the configured second full page-load attempt actually runs instead of surfacing `extractor retry limit exceeded` immediately.
- Provider tiles now keep showing the last successful metric rows when a later refresh fails, with an `error · stale` status so the stale values remain useful but clearly marked.
- Claude and Codex no longer treat incidental `Cloudflare` text inside normal signed-in page content as a browser-check interstitial. The shared security-verification detector now requires stronger challenge signals and ignores pages with usage-page evidence, preventing false `Click Connect` auth errors on valid usage pages.

## 0.5.6 - 2026-05-18

### Changed

- Claude and Codex now share a single `ScrapeRunner` that drives the headless scraper and retries the whole scrape once when the snapshot builder reports a transient layout error. Codex previously had no build-level retry, so a half-rendered analytics page would surface as an immediate error instead of recovering on the second pass.
- The Claude extractor now polls inside the same page load when the usage panel hasn't rendered yet (no `%` and no `Plan usage limits` text). This reuses the existing `__retry_after_ms` path that Codex uses for the Personal usage tab, so a slow-hydrating panel no longer requires tearing down the page and reloading it.
- Shared helpers (`normalize_percent`, `idle_session_weekly_metrics`, `is_security_verification_page`) are factored out of the Claude and Codex providers into `providers/_common.py` so the two paths stop drifting.

### Fixed

- Reduced AI Gauge log noise on Claude scrapes. claude.ai's `[IsolatedSegment]` analytics iframe and Datadog RUM bundle previously produced ~25 info-level console lines per refresh; those fragments are now filtered, and remaining JS info-level console messages are routed to DEBUG so only warnings and errors from the embedded pages reach the file log. Warnings, errors, and AI Gauge's own scrape lifecycle lines are unchanged.
- Cookie hydration no longer overwrites Chromium's persisted cookies for an account on every startup. The keyring's stored blob is only re-injected when the profile has no `Cookies` file yet (first launch after Paste cookie, or a wiped profile). Once Chromium has its own cookie store on disk, session tokens that the site rotates mid-session now survive AI Gauge restarts instead of being clobbered back to the original paste, which had been causing pasted-cookie accounts to drift into a "Sign in" state after a few restarts.

## 0.5.5 - 2026-05-07

### Changed

- OpenRouter model breakdown rows now strip the provider prefix (e.g. `anthropic/`, `openai/`, `google/`) from the displayed name and truncate names longer than 20 characters with an ellipsis. The full original slug is preserved in the row tooltip.
- OpenRouter model breakdown bars now line up at the same x position by sizing all model rows to a uniform label column width.

### Fixed

- Fixed Codex personal usage scraping after ChatGPT started dropping the `#personal-usage` fragment and defaulting the analytics page to Workspace usage. AI Gauge now selects the Personal usage tab inside the rendered page, waits for the tab content to hydrate, and only verifies Codex sessions once the actual personal usage rows are visible.

## 0.5.4 - 2026-05-07

### Added

- Claude and Codex now support multiple named accounts. Add extra accounts from the dedicated Claude or Codex Settings tab; each account gets its own browser profile, cookie storage, tile, snapshot history, and display name such as `Codex (Account 2)`.
- Settings now separates provider visibility from account management: General controls whether Claude/Codex groups appear, while the Claude and Codex tabs manage account names, sign-in, cookie paste, add, and remove actions.

### Changed

- The main widget now groups multiple Claude accounts before Codex accounts, keeps secondary account names visible in expanded and compact views, wraps compact chips onto additional rows when needed, and uses a scrollable dark tile area when many accounts are shown.
- Codex/OpenAI sign-in guidance now explicitly calls out Google and passkey accounts: use Paste cookie when the embedded browser cannot complete the Google/passkey flow.

### Fixed

- Fixed the multi-account widget scroll area inheriting Qt's default light background.
- Fixed secondary-account Settings rows being cramped in a single mixed provider list.

## 0.5.3 - 2026-05-06

### Added

- OpenRouter support with separate storage for the standard inference key and management key, plus settings for enabling the provider and optionally setting a daily spend budget.
- OpenRouter diagnostics now log non-secret endpoint status for `/credits`, `/key`, and `/activity`, including whether each key type is configured, payload field names, and activity row counts.

### Changed

- OpenRouter balance and spend now render as a single split row, e.g. `Balance $11.16 left` with `Spend today $0.00 / month $0.00` right-aligned; UTC details moved to the tooltip.
- OpenRouter daily spend only renders as a gauge when a daily budget is configured.
- OpenRouter model breakdown now uses the default `/activity` history window, shows up to six models, and labels it explicitly as `Models: last 30 completed UTC days`.
- OpenRouter refreshes before browser-scraped providers so its API-backed tile does not wait behind Claude/Codex page loads.
- Note-only OpenRouter rows, such as empty completed-day model activity, no longer render as empty gauges with `--`.
- Routine successful OpenRouter refresh diagnostics now log at debug level instead of filling the normal log on every refresh.

### Fixed

- OpenRouter `/activity` now uses the management key, matching OpenRouter's current API requirements, instead of incorrectly using the standard inference key and receiving HTTP 403 responses.
- OpenRouter management endpoints are skipped when no management key is configured, with visible tile guidance instead of failed background calls.

## 0.5.2 - 2026-05-03

### Added

- Lifecycle diagnostics: AI Gauge now writes a five-minute heartbeat plus explicit Qt `aboutToQuit` and Python `atexit` log lines with uptime, UI mode, enabled providers, in-flight refreshes, queued providers, next refresh delay, and idle-backoff count. This should make future unexplained exits easier to distinguish from clean quits, OS shutdowns, and mid-refresh process termination.

### Changed

- Settings is now more compact: general/window/provider controls share a shorter tab, GitHub Copilot details live on their own tab, and long helper text was tightened so the dialog fits more comfortably on smaller displays.

## 0.5.1 - 2026-04-30

### Added

- Pace indicator on every active time window: provider tile bars get a thin tick at the elapsed-time position, and compact-view chips get a small downward-pointing notch on the top edge, so quota used vs. elapsed session/weekly/monthly time is visible at a glance.
- **macOS and Linux support.** A new `aigauge.platforms` seam routes per-OS work (app-data directory, secret storage, auto-start) through `WindowsPlatform` / `MacOSPlatform` / `LinuxPlatform` impls. Windows behavior is unchanged.
- **Stats-style menu-bar UI on macOS.** Instead of the floating widget, macOS shows one tinted dot + percent per enabled provider directly in the menu bar (`● 42% ● 78% ● 15%`). Clicking opens the panel as a popover anchored under the menu-bar item; clicking outside dismisses. The pixmap is rendered at 2× DPR for Retina.
- **No-tray fallback on Linux.** Stock GNOME has no system tray; AI Gauge now detects this via `QSystemTrayIcon.isSystemTrayAvailable()`, keeps the floating widget visible, and serves the same Show / Refresh / Settings / Quit menu via right-click on the widget.
- **Cross-platform CI.** `test.yml` now runs on `windows-latest`, `macos-latest`, and `ubuntu-22.04` across Python 3.11 and 3.12. `release.yml` builds per-OS artifacts in parallel and attaches them to a single draft release.
- `build.sh` for macOS / Linux PyInstaller builds. On macOS it injects `LSUIElement=true` into the bundle's `Info.plist` so the `.app` runs as a menu-bar agent without a Dock icon.

### Changed

- The `start_with_windows` config field is renamed to `start_at_login` (with automatic migration); the matching Settings checkbox now reads "Start at login". UI strings that called out "Windows Credential Manager" now say "system keychain".
- Per-OS secret backends: macOS uses Keychain via `keyring`, Linux uses Secret Service via `keyring`, Windows keeps the existing DPAPI sidecar for cookies (Credential Manager's blob limit is too small for ChatGPT JWTs).
- Per-OS auto-start: LaunchAgent plist on macOS, `~/.config/autostart/ai-gauge.desktop` on Linux, the existing Run-key entry on Windows.
- App-data directory is now per-OS: `~/Library/Application Support/ai-gauge` on macOS, `$XDG_CONFIG_HOME/ai-gauge` on Linux, unchanged `%APPDATA%/ai-gauge` on Windows.

### Fixed

- Copilot monthly resets are now anchored to UTC midnight on the first of the month, so countdowns near month end match GitHub's reset boundary instead of local midnight.
- Claude scrapes now retry transparently after a wake-from-sleep timeout instead of giving up on the first attempt: the headless scraper retries up to twice on `timeout`, `page failed to load`, or null-extractor results, so a cold network on the first refresh after resume usually succeeds on the retry instead of surfacing as `error · timeout`.
- Claude usage panel that hadn't finished rendering when the extractor ran is no longer misclassified as the idle 0%/0% state. The signed-in-but-empty heuristic now requires positive evidence the usage panel rendered (the "Plan usage limits" header in the body) before declaring idle, and `ClaudeProvider` retries the whole scrape once on a transient layout-error result so the second attempt sees the populated rows.

## 0.5.0 - 2026-04-28

### Changed

- Renamed the project from `usage-view` to `ai-gauge`. Package import is now `aigauge`, console script is `ai-gauge`, app data lives under `%APPDATA%/ai-gauge/`, and the standalone build outputs `dist/ai-gauge/ai-gauge.exe`. Existing installs that wrote to `%APPDATA%/usage-view/` are not migrated automatically — copy the folder over if you want to keep history and saved sessions.

### Added

- Continuous integration on GitHub Actions: pytest runs against Python 3.11 and 3.12 on Windows for every push and pull request, gated by a `tools/check_versions.py` script that fails the build if `pyproject.toml`, `src/aigauge/__init__.py`, the README, and the changelog drift out of sync.
- Automated release workflow: pushing a `v*` tag spins up a Windows runner that runs the tests, builds the standalone `.exe` via `build.ps1`, zips `dist/ai-gauge/`, computes a SHA256, and attaches both files to a draft GitHub Release for review.
- Issue templates (bug report, provider layout broken, feature request) and a `CONTRIBUTING.md` with dev setup, test, and PR expectations.
- URL allowlist on the embedded sign-in browser: navigation is restricted to the auth-related domains for Claude and ChatGPT (and their known OAuth/identity hops). Off-allowlist navigations are blocked, hardening the embedded browser against open-redirect abuse on either provider's auth flow.

### Security

- `secret_storage` now refuses to write secrets on non-Windows hosts instead of silently falling back to a plaintext `secrets.dat` (an artifact of early cross-platform dev). Reads still succeed where possible so existing test fixtures keep working, but production write paths require DPAPI.
- `SECURITY.md` now spells out that DPAPI encryption is per-user, not per-process: any code running as the same Windows user can decrypt `secrets.dat`.

## 0.4.3 - 2026-04-28

### Added

- Added a single-instance lock so a Startup launch and a manual launch cannot run two full app trees at the same time.

### Fixed

- Fixed completed Claude/Codex offscreen scrapes retaining their `QWebEnginePage` owner, which could leave QtWebEngine renderer processes accumulating after repeated refreshes.
- Cleaned up cookie-verification WebEngine pages and OAuth popup windows more aggressively after they finish or close.

## 0.4.2 - 2026-04-28

### Added

- Persisted compact pill mode: header collapse button shrinks the panel to a single row of provider chips showing session percent, with severity-tinted fills and a one-click expand back to the full panel. Mode is saved across restarts.
- Indeterminate "skeleton" bars on provider tiles before the first snapshot arrives so a fresh launch shows animated placeholders instead of empty rows.
- Provider diagnostics logging: Claude/Codex page classifications (logged out, security verification, empty signed-in usage, layout changed, load failed) and Copilot API failure modes (missing PAT, unresolved username, HTTP errors with request id, unexpected exceptions) now emit structured log lines for support triage.

### Changed

- Scheduled and manual refreshes now keep existing tile values visible and just dim the tile while a new scrape runs, instead of resetting rows to `loading...`. Each tile un-dims as its own snapshot arrives.
- Settings dialog is now non-modal: it can stay open while the user interacts with the main panel or browser, and clicking the status panel raises the existing Settings window instead of opening a second one.
- Always-on-top suspension is reference counted, so overlapping suspensions (Settings + cookie paste + sign-in) no longer race and leave the panel pinned.
- Tile severity color bands shifted to 95% / 80% / 60% thresholds with a paired darker tone used for compact-mode chip fills so colors stay readable under white text.
- Claude's own "Can't reach Claude" interstitial is now reported as a load failure instead of a layout-changed scraper error.

## 0.4.1 - 2026-04-28

### Changed

- The next refresh is now pulled forward to shortly after a known reset time so the panel updates promptly when a session/weekly limit rolls over, instead of showing 100% for the full idle backoff.
- Fresh installs no longer auto-open Settings; the panel just shows provider tiles in their auth-required state with a Sign in button.
- Settings and cookie paste dialogs no longer pin themselves above other windows. While any of Settings, cookie paste, or sign-in is open, the main panel also drops out of always-on-top so the user can switch to their normal browser to grab a cookie or click a magic-link email.

## 0.4.0 - 2026-04-28

### Changed

- Scheduled refreshes now keep the existing tile values visible until fresh results arrive; only manual refreshes clear rows to `loading...`.
- The header now shows a live countdown to the next scheduled refresh instead of only the configured cadence.
- Widget width is fixed at the compact 340 px panel size, and height is clamped on load/refit/save so cross-monitor DPI changes cannot stretch the panel into an oversized banner.
- Claude signed-in pages with no usage yet now show idle zero rows instead of a layout-changed error.
- Codex signed-in pages with no usage yet now show idle zero rows instead of a layout-changed error.
- Claude weekly resets like `Mon 6:00 PM` are now parsed and displayed even when weekly usage is still 0%.
- Codex signed-out pages are now classified as `not signed in` instead of `layout changed` when the login page omits the expected link selector.
- Cloudflare / `Just a moment...` interstitials are now classified as authentication required instead of a generic layout error.
- Unused limits with no parsed reset time now consistently show `idle` instead of a blank reset label.
- Provider errors now log a compact sanitized raw payload summary.
- GitHub Copilot PATs now live only in Windows Credential Manager; legacy fallback-file PATs are migrated when possible. Settings can clear the saved PAT.
- PyInstaller builds now use `--clean` and `--noupx` to avoid stale bundles.

### Notes

- An external-Chrome / CDP refresh path was prototyped during 0.4 development and removed before release: Cloudflare's `cf_clearance` cookie expires every ~30 min on bot-flagged sessions and cannot be renewed from a non-interactive Chrome process, regardless of launch flags. Claude and Codex continue to refresh through an in-process `QWebEnginePage` whose cookie jar is kept warm between scrapes.

## 0.3.1 - 2026-04-27

### Changed

- Re-enabled provider tiles now return to the stable Claude, Codex, Copilot order instead of appearing at the bottom until restart.
- Refresh now immediately clears visible provider rows back to `loading...` so manual refreshes show progress while scans run.
- Codex's short-window usage label now displays as `Session` to match Claude.

### Fixed

- Existing in-flight Codex history using the old `5 hour` label is migrated to `Session`.

## 0.3.0 - 2026-04-27

### Added

- Verify-on-paste: pasting a cookie now loads the actual usage page and reports back whether the session authenticates, naming likely causes when it doesn't.
- Clickable error labels: provider tiles now show short reasons (`error · timeout`, `error · layout changed`, etc.) and open a details dialog with the raw payload, copy button, and shortcut to the log folder.
- Rotating diagnostic log at `%APPDATA%/ai-gauge/ai-gauge.log` with an "Open log folder" button in Settings.
- Refresh-cadence indicator in the widget header showing active vs idle mode and the current interval.
- Loading state for provider tiles before their first snapshot arrives.
- Per-period usage history: peak percent reached for each session/weekly/monthly window is appended to `history.jsonl` on rollover, with in-flight state in `current.json`. No UI yet — pure background record-keeping.

### Changed

- Widget panel height now auto-fits the visible providers — toggling a provider off shrinks the panel rather than leaving blank space.
- Copilot monthly quota changes now update the displayed metric immediately instead of waiting for the next refresh.
- Provider settings now include a hint and tooltips explaining that unchecking hides the tile from the panel.
- Cookie paste verifies the imported session before accepting it.

## 0.2.0 - 2026-04-27

### Added

- Added app version display in the widget header, tray tooltip, and Qt application metadata.
- Added adaptive auto-refresh with separate active and max intervals.
- Added opt-in Start with Windows support.
- Added Copilot plan/quota selection with common plan defaults and a Custom fallback.
- Added a PyInstaller launcher for reliable packaged builds.

### Changed

- Codex and Claude cookie setup now prefer full `Cookie:` request headers and validate provider-specific auth cookies.
- Copilot usage now tracks included premium requests consumed instead of billable overage.
- Claude and Codex unused limits now show `idle` instead of misleading future reset times.
- Provider refreshes now run sequentially to reduce peak background CPU, memory, and network usage.
- Default max auto-refresh interval is now 60 minutes, with 5 minutes as the active cadence.

### Fixed

- Fixed ChatGPT split-cookie handling.
- Fixed Claude weekly-limit extraction.
- Fixed PyInstaller one-file relative-import crash.
- Filtered noisy QtWebEngine console messages from third-party pages.
- Cleaned up offscreen WebEngine views after scrape completion.

## 0.1.0 - 2026-04-27

### Added

- Initial Windows tray/widget app for Claude.ai, ChatGPT Codex, and GitHub Copilot usage.
- Added DPAPI-backed secret storage, persistent WebEngine profiles, settings dialog, cookie paste flow, and test coverage for core helpers.
