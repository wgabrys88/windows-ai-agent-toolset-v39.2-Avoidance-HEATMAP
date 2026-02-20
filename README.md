```
FRANZ AGENT -- ARCHITECTURE REPORT
====================================
Comparison: Original Architecture vs. Current Architecture
-----------------------------------------------------------


OVERVIEW
--------
Franz is a stateless narrative agent that controls a Windows computer via
visual language model (VLM) inference. The agent describes what it sees,
decides what to do, and the system executes those actions physically.
The core philosophical principle is unchanged: the less context the model
carries, the more self-aware and focused its decisions become. The agent
tells itself a story one turn at a time.


FILE INVENTORY
--------------

Original:
    config.py       -- runtime constants
    main.py         -- orchestration loop
    execute.py      -- action extractor and executor
    tools.py        -- Win32 mouse/keyboard/memory primitives
    capture.py      -- screen capture, crop, resize, PNG encode
    annotate.py     -- server-side PNG pixel annotator (subprocess)
    panel.py        -- HTTP proxy, SSE broadcaster, dashboard server
    panel.html      -- browser dashboard with canvas annotation overlay

Current:
    config.py       -- runtime constants (unchanged)
    main.py         -- orchestration loop (unchanged)
    execute.py      -- action extractor and executor (unchanged)
    tools.py        -- Win32 mouse/keyboard/memory primitives (unchanged)
    capture.py      -- screen capture, crop, resize, PNG encode (restored)
    panel.py        -- HTTP proxy, SSE broadcaster, dashboard server (modified)
    panel.html      -- browser dashboard with iframe to canvas (modified)
    canvas.html     -- NEW: HTML5 canvas renderer, annotator, heat layer

    DELETED:
    annotate.py     -- entire file removed (~320 lines eliminated)


ORIGINAL ARCHITECTURE
---------------------

Turn flow:

  execute.py
    captures screen (full res -> crop -> resize 512x288 -> PNG -> b64)
    runs previous actions physically
    returns screenshot_b64 to main.py

  main.py
    sends story + screenshot to proxy (panel.py) via HTTP

  panel.py (proxy)
    intercepts request
    reads _prev_actions from previous turn
    calls annotate.py subprocess with (image_b64, actions)

  annotate.py (subprocess)
    decodes PNG to RGBA pixel grid in pure Python
    draws red markers using Bresenham line algorithm, midpoint circle,
    hand-rolled arrowhead -- all pixel-level manipulation
    re-encodes to PNG using struct + zlib
    returns annotated b64 to panel.py

  panel.py
    swaps annotated image into request
    forwards to LM Studio (port 1235)
    parses VLM response for new actions
    stores actions for next turn
    logs turn, saves screenshot PNG, broadcasts via SSE

  panel.html
    receives SSE turn data
    fetches screenshot from /turn/N/screenshot
    draws its own canvas overlay (second independent annotation layer)
    shows in BR quadrant

Problems with original architecture:
  - annotate.py decoded and re-encoded the PNG in pure Python for every turn
    (slow, ~300 lines of pixel math, filter reconstruction, zlib roundtrip)
  - capture.py did crop and resize internally, sending small image to proxy
    but annotate.py had no knowledge of this sizing pipeline
  - two annotation systems existed independently:
    server-side (annotate.py bakes markers into PNG sent to VLM)
    client-side (panel.html canvas overlay for display only)
    these could drift out of sync
  - no visual heatmap of action history
  - no auto-pause on startup, browser opened manually
  - logging wrote batched JSON files with image_data_uri fields (enormous)
  - 10 second wait before main.py launch with no pause gate at startup


CURRENT ARCHITECTURE
--------------------

Turn flow:

  execute.py
    runs previous actions physically
    calls capture.py subprocess

  capture.py
    captures full screen via BitBlt (GDI, DPI-aware)
    applies crop if crop.json exists (pixel coordinates on full screen)
    resizes to config WIDTH x HEIGHT (default 512x288) via StretchBlt HALFTONE
    encodes to PNG using struct + zlib
    returns screenshot_b64 (correctly sized, cropped)

  main.py
    sends story + screenshot to proxy (panel.py) via HTTP

  panel.py (proxy)
    intercepts request
    extracts image_b64 from request
    reads _prev_actions (actions parsed from previous VLM response)
    dispatches render job to canvas.html:
      { seq, image_b64, actions, render_w, render_h }
    blocks on threading.Event waiting for /annotated POST back
    (no timeout -- waits indefinitely, browser always open)

  canvas.html (iframe in dashboard BR quadrant)
    polls /render_job every 200ms
    receives job with already-correctly-sized image
    decodes PNG via createImageBitmap (GPU, native browser)
    draws action image onto canvas via drawImage (one call, GPU accelerated)
    draws heat layer UNDER action markers:
      for each click/right_click/double_click:
        radial gradient centered on action point
        radius = 22% of min(width, height) ~= 63px on 288px tall image
        center: rgba(255, 40, 0, 0.88) -> transparent at edge
        drawn on offscreen canvas with globalCompositeOperation = lighter
        overlapping actions compound naturally
      for each drag:
        endpoint blobs (same radial gradient as clicks)
        intermediate blobs along path every 0.4 * radius steps
        slightly smaller radius (0.75x) for path fill
        result: capsule/stadium shape covering the drag trajectory
    draws crisp white action markers ON TOP of heat layer
      (white instead of red so markers read clearly over orange heat)
    calls canvas.toBlob -> FileReader -> b64
    POSTs annotated b64 to /annotated

  panel.py (proxy, continued)
    receives annotated b64 from /annotated
    sets threading.Event (unblocks waiting turn thread)
    swaps annotated image into original VLM request
    forwards to LM Studio (port 1235)
    VLM sees: correctly sized + cropped image WITH heat overlay AND markers
    parses VLM response for new actions
    stores new actions in _prev_actions for next turn
    logs turn to turns.jsonl (no image_data_uri in log)
    saves annotated PNG to turn_NNNN.png
    broadcasts turn data via SSE (no image_data_uri)

  panel.html
    receives SSE turn data
    updates TL (story text), TR (VLM output), BL (turn metadata + actions)
    BR quadrant is an iframe pointing at /canvas
    canvas.html is already live and will show the next job automatically
    no screenshot fetch, no second canvas overlay, no duplication

  Startup sequence:
    panel.py writes PAUSED file immediately on init
    opens browser automatically via webbrowser.open()
    launches main.py after 3 second delay
    main.py hits PAUSE_FILE check and waits
    user selects crop region in dashboard, clicks Resume
    PAUSE_FILE removed, agent begins running


KEY DIFFERENCES
---------------

Feature                  Original                    Current
-----------------------  --------------------------  ---------------------------
Annotation engine        annotate.py subprocess      canvas.html (browser GPU)
                         pure Python pixel loops     createRadialGradient + drawImage
                         Bresenham, midpoint circle  native Canvas 2D API

Annotation location      server-side only            browser renders, POSTs back
                         PIL-style pixel mutation    proxy swaps result into request

Heatmap                  none                        Gaussian radial gradients
                                                     clicks: circular blob
                                                     drags: capsule along path
                                                     lighter compositing = additive
                                                     strong enough to visually
                                                     discourage re-clicking same area

Marker color             red on clean screenshot     white over orange heat blob
                                                     readable contrast maintained

Image sizing             capture.py: crop + resize   capture.py: crop + resize
                         annotate.py: unaware of     canvas: receives pre-sized image
                         output dimensions           no second resize needed

Dashboard BR quadrant    img + canvas overlay        iframe to canvas.html
                         two annotation systems      single source of truth
                         fetches screenshot per turn always showing latest job

Logging                  batched JSON files          single turns.jsonl
                         includes image_data_uri     text-only: story, vlm_text,
                         fields (enormous files)     actions, metadata, usage

Screenshot saved         raw unannotated image       annotated image (what VLM saw)
                         before annotation           saved after canvas roundtrip

Startup                  manual browser open         automatic webbrowser.open()
                         10s fixed wait              starts PAUSED, user resumes
                         no pause at launch          after selecting crop region

Deleted code             annotate.py exists          annotate.py deleted
                         ~320 lines pixel math       zero Python pixel manipulation


HEATMAP DESIGN RATIONALE
------------------------
The Qwen3-VL 2B model is a visual model. It does not need a text instruction
saying "avoid coordinate (500, 300)". It needs to see that region as visually
occupied, hot, or dangerous. A strong radial gradient centered on the previous
action point achieves this by:

  1. Making the area look "already touched" or "burned"
  2. Covering a proportionally large region (~22% of image dimension as radius)
     so that nearby misclicks also fall in the warm zone
  3. Using additive compositing (lighter) so multiple clustered actions
     produce a brighter, larger region naturally without any grid math
  4. Placing crisp white markers on top so the exact action points remain
     readable while the surrounding area is visually suppressed

For drags, the stadium shape ensures the model avoids not just the endpoints
but the entire trajectory, which is appropriate since a drag represents
intentional interaction with a region, not a point.

This is a one-turn memory: actions from turn N burn onto the image seen at
turn N+1. The next turn starts clean. No accumulation, no decay needed.
Simple, deterministic, zero configuration.


NORMALIZED COORDINATE SYSTEM
-----------------------------
All action coordinates are 0-1000 on both axes throughout the system.
The mapping to pixels happens at two distinct points:

  tools.py _remap():
    0-1000 -> physical screen pixels (with crop offset applied)
    used when executing actions physically via Win32 SendInput

  canvas.html drawHeat() / drawMarkers():
    0-1000 -> canvas pixels (image width/height after capture.py sizing)
    args[0] * w / 1000, args[1] * h / 1000
    used when rendering annotations visually

Crop is applied in capture.py at capture time. The VLM sees the cropped
and resized image. Coordinates the VLM outputs therefore refer to positions
within the cropped region. tools.py remaps these back to absolute screen
coordinates using the stored crop x1/y1 origin.

This means the coordinate system is consistent end-to-end: the model sees
coordinate (500, 500) as the center of whatever region was selected in the
crop overlay, and clicking (500, 500) physically moves the cursor to the
center of that same region on screen.
```
