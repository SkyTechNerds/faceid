# Russian web interface

Both the current and classic web interfaces include a language selector in the
header. On the first visit, a Russian browser locale selects Russian; other
locales use English. Select **Русский** or **English** to override that choice.
The choice is shared by both interfaces and is stored in this browser's local
storage, not in the FaceID configuration.

The translation covers menus, settings, help text, result dialogs, notifications,
gallery checks, history, backups and diagnostic tools. Russian labels can wrap on
narrow screens. The translation layer observes new text and supported tooltip
attributes when the interface renders or updates a result.

The three static UI files are preserved byte for byte from the user-verified
0.27.1-ru.10 local build, including its layout and browser language selection.
The classic settings tab grouping includes the wrapped fallback section, and
unknown history entries do not show the action for analysing an incorrect known
person match. These are UI fixes in that tested build, not recognition changes.

This is an optional presentation layer over the existing English interface; it
does not introduce a server-side locale or modify API requests. Recognition
thresholds, camera configuration, MQTT topics and payloads, gallery data, and
automation behavior retain their upstream defaults and existing user values.
Log lines, code examples, editable values and unknown diagnostics retain their
original content. New or changed English UI messages need corresponding dictionary
entries or complete-message patterns in `static/assets/i18n-ru.js`.

After editing the UI or translation, run `scripts/sync-addon.sh` so the standalone
and Home Assistant build contexts stay identical. No app version bump is included
in the localization change.

## Validation

These tests require Node.js and Python 3 only; they do not load face models, use a
camera, contact a broker, or need a running Home Assistant instance:

```sh
node --check static/assets/i18n-ru.js
node tests/test_ui_translation.cjs
node tests/test_ui_translation_dom.cjs
node tests/test_ui_translation_settings.cjs
```

The fixture runner executes both interfaces' render functions and action handlers
against synthetic API data, then checks their visible text and tooltip attributes.
The DOM fixture checks language restoration, repeated and dynamic translation,
technical data, form values, and the return values of native dialogs. These are
programmatic checks, not a substitute for browser layout and accessibility review.
The Russian translation used as the starting point was also tested by its user in
a local Home Assistant installation.
