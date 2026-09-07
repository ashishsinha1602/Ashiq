# studio

The Studio page. `schemagate.js` is a JavaScript port of the selector so the
hosted demo runs in the browser with no server; `tests/test_js_parity.py`
holds it to identical rankings with the Python library.

    npm install blakejs        # once, for the parity test under node
    python studio/build.py     # rebuilds studio.html into src/schemagate/
