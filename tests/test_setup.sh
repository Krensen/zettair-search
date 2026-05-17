#!/usr/bin/env bash
# tests/test_setup.sh — exercise the staleness logic in deploy/setup.sh.
#
# Each scenario sets up a sandbox, pre-populates files with controlled
# mtimes, runs setup.sh in DRY_RUN=1 mode, and asserts on the
# `DECISION: rebuild X` / `DECISION: skip X` lines. We're testing the
# decision logic, not the actual rebuild commands — those are stubbed
# out by DRY_RUN.
#
# Run from the repo root or from this dir; both work:
#   bash tests/test_setup.sh
#
# Exits 0 on all-pass, 1 on any failure. Per-scenario output goes to
# stdout in colour (if a tty), with PASS/FAIL prefixes that grep cleanly.

set -u  # not -e — we want to keep going past failed assertions

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
SETUP_SH="$REPO_DIR/deploy/setup.sh"
SANDBOX_ROOT=/tmp/zettair-setup-tests
PASS=0
FAIL=0
FAILED_TESTS=()

# Colour output if stdout is a terminal
if [ -t 1 ]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
    GREEN=""; RED=""; YELLOW=""; BOLD=""; RESET=""
fi

# ── Helpers ────────────────────────────────────────────────────────────────

# Set up an isolated sandbox for one scenario.
sandbox_setup() {
    local name="$1"
    SANDBOX="$SANDBOX_ROOT/$name"
    rm -rf "$SANDBOX"
    mkdir -p \
        "$SANDBOX/volume/wikiindex" \
        "$SANDBOX/install/zettair-search/deploy" \
        "$SANDBOX/install/zettair/devel/.libs" \
        "$SANDBOX/install/zettair/wikipedia" \
        "$SANDBOX/install/zettair-search/logs" \
        "$SANDBOX/etc"

    # Provide a fake systemd unit file so the cp step finds source.
    touch "$SANDBOX/install/zettair-search/deploy/zettair-search.service"

    # Mark the box as already-provisioned so the apt/useradd block
    # is skipped for tests that don't care about it.
    touch "$SANDBOX/etc/setup_marker"

    # Pre-create some "previous-run" baselines: zettair repo with a
    # real commit hash for HEAD comparisons. We fake it with a file.
    mkdir -p "$SANDBOX/install/zettair/.git"
    mkdir -p "$SANDBOX/install/zettair-search/.git"
}

# Run setup.sh in dry-run mode against the current sandbox.
# Returns the full output (decisions + dry-run command list + log lines).
# stderr is mixed into stdout for grepping convenience.
run_setup() {
    DRY_RUN=1 \
    INSTALL_DIR="$SANDBOX/install" \
    VOLUME="$SANDBOX/volume" \
    SETUP_MARKER_OVERRIDE="$SANDBOX/etc/setup_marker" \
    DEPLOY_USER="$(id -un)" \
    SERVICE_USER="$(id -un)" \
    bash "$SETUP_SH" 2>&1 || true
}

# Stub git so clone creates the destination dir and pull/rev-parse
# are no-ops. setup.sh no longer compares HEADs; it uses
# source-mtime-vs-binary-mtime.
make_git_shim() {
    SHIM_DIR="$SANDBOX/shim"
    mkdir -p "$SHIM_DIR"
    cat > "$SHIM_DIR/git" <<'HEREDOC'
#!/usr/bin/env bash
case "$1" in
    clone)
        for arg; do dest="$arg"; done
        mkdir -p "$dest/.git" "$dest/devel/.libs" "$dest/wikipedia" "$dest/deploy"
        ;;
esac
exit 0
HEREDOC
    chmod +x "$SHIM_DIR/git"
    export PATH="$SHIM_DIR:$PATH"
}

# Assertions

assert_decided() {
    local artefact="$1"
    if echo "$OUTPUT" | grep -q "^DECISION: rebuild $artefact "; then
        return 0
    else
        echo "  ${RED}FAIL${RESET}: expected 'rebuild $artefact' in decisions"
        echo "$OUTPUT" | grep "^DECISION:" | sed 's/^/      /'
        return 1
    fi
}

assert_skipped() {
    local artefact="$1"
    if echo "$OUTPUT" | grep -q "^DECISION: skip $artefact "; then
        return 0
    else
        echo "  ${RED}FAIL${RESET}: expected 'skip $artefact' in decisions"
        echo "$OUTPUT" | grep "^DECISION:" | sed 's/^/      /'
        return 1
    fi
}

assert_not_decided() {
    local artefact="$1"
    if echo "$OUTPUT" | grep -q "^DECISION: rebuild $artefact "; then
        echo "  ${RED}FAIL${RESET}: did not expect 'rebuild $artefact' in decisions"
        echo "$OUTPUT" | grep "^DECISION: rebuild $artefact " | sed 's/^/      /'
        return 1
    fi
    return 0
}

# Run a scenario: scenario name + body function name.
scenario() {
    local name="$1"; shift
    local body="$1"
    echo "${BOLD}── $name${RESET}"
    sandbox_setup "$name"
    local local_pass=1
    if "$body"; then
        echo "  ${GREEN}PASS${RESET}: $name"
        PASS=$((PASS+1))
    else
        echo "  ${RED}FAIL${RESET}: $name"
        FAIL=$((FAIL+1))
        FAILED_TESTS+=("$name")
    fi
    echo
}

# ── Scenario helpers (build canonical states) ──────────────────────────────

# Populate a "fully built" state: TREC, index + 3 sidecars, click_prior,
# autosuggest, docstore, urls, plus all clickstream files. mtimes are
# set in dependency order so nothing is stale.
populate_fully_built() {
    local trec="$SANDBOX/volume/enwiki_top1m.trec"
    local titles="$SANDBOX/volume/top_titles.txt"
    local idx_dir="$SANDBOX/volume/wikiindex"
    local wiki_dir="$SANDBOX/install/zettair/wikipedia"

    # Older: clickstream files.
    for m in 2024-01 2024-02 2024-03 2024-04 2024-05 2024-06 \
             2024-07 2024-08 2024-09 2024-10 2024-11 2024-12 \
             2025-01 2025-02 2025-03; do
        touch -t 202401010000 "$wiki_dir/clickstream-enwiki-${m}.tsv.gz"
    done

    # Then TREC + titles
    touch -t 202401020000 "$titles"
    touch -t 202401020100 "$trec"

    # Then index + sidecars (zet -i emits all in one pass)
    touch -t 202401030000 "$idx_dir/index.param.0"
    touch -t 202401030000 "$idx_dir/index.field_lengths"
    touch -t 202401030000 "$idx_dir/index.field_stats"
    touch -t 202401030000 "$idx_dir/index.docno_map.tsv"

    # Then click_prior, autosuggest, docstore, urls
    touch -t 202401040000 "$idx_dir/index.click_prior.bin"
    touch -t 202401040000 "$SANDBOX/volume/autosuggest.json"
    touch -t 202401040000 "$SANDBOX/volume/enwiki_top1m.docstore"
    touch -t 202401040000 "$SANDBOX/volume/enwiki_top1m_urls.store"
    touch -t 202401040000 "$SANDBOX/volume/enwiki_top1m_urls.map"

    # PRD-027: reading sidecar (derived from docstore; same mtime fine)
    touch -t 202401040000 "$SANDBOX/volume/enwiki_top1m.reading.bin"

    # zet binary (ELF-ish — we fake the file file-magic check by
    # making it a real ELF if possible, or by stubbing `file`)
    cat > "$SANDBOX/install/zettair/devel/zet" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$SANDBOX/install/zettair/devel/zet"

    # Stub `file` to claim it's ELF (since we're on macOS, real bash
    # scripts won't pass the test).
    SHIM_DIR="$SANDBOX/shim"
    mkdir -p "$SHIM_DIR"
    cat > "$SHIM_DIR/file" <<'EOF'
#!/usr/bin/env bash
echo "$1: ELF 64-bit LSB executable"
EOF
    chmod +x "$SHIM_DIR/file"
    export PATH="$SHIM_DIR:$PATH"

    # zettair libraries
    touch "$SANDBOX/install/zettair/devel/.libs/zet"
    touch "$SANDBOX/install/zettair/devel/.libs/libzet.so.0"

    make_git_shim
}

# ── Scenarios ──────────────────────────────────────────────────────────────

s01_fresh_box() {
    # Truly empty — wipe what sandbox_setup pre-created.
    rm -f "$SANDBOX/etc/setup_marker"
    rm -rf "$SANDBOX/install/zettair" "$SANDBOX/install/zettair-search"
    # We still need a tiny shim because the script will try to clone
    # via `git`. The shim returns success and we manually create the
    # cloned dirs to simulate a successful clone.
    SHIM_DIR="$SANDBOX/shim"
    mkdir -p "$SHIM_DIR"
    cat > "$SHIM_DIR/git" <<'EOF'
#!/usr/bin/env bash
# Simulate a successful clone by creating the destination directory.
case "$1" in
    clone)
        # last arg is the target dir
        for arg; do dest="$arg"; done
        mkdir -p "$dest/.git" "$dest/devel/.libs" "$dest/wikipedia" "$dest/deploy"
        ;;
esac
exit 0
EOF
    chmod +x "$SHIM_DIR/git"
    export PATH="$SHIM_DIR:$PATH"

    OUTPUT=$(run_setup)
    local ok=1
    assert_decided system-packages || ok=0
    assert_decided clone-zettair-search || ok=0
    assert_decided clone-zettair || ok=0
    assert_decided zet-binary || ok=0
    assert_decided bz2-download || ok=0
    assert_decided trec || ok=0
    assert_decided index || ok=0
    assert_decided click-prior || ok=0
    assert_decided autosuggest || ok=0
    assert_decided docstore || ok=0
    assert_decided reading-sidecar || ok=0
    assert_decided systemd-unit || ok=0
    [ $ok -eq 1 ]
}

s02_no_op() {
    # Fully built, nothing changed — only systemd-unit + service-restart
    # should be decided to rebuild.
    populate_fully_built
    OUTPUT=$(run_setup)
    local ok=1
    assert_skipped system-packages || ok=0
    assert_skipped zet-binary || ok=0
    assert_skipped index || ok=0
    assert_skipped click-prior || ok=0
    assert_skipped autosuggest || ok=0
    assert_skipped docstore || ok=0
    assert_skipped reading-sidecar || ok=0
    assert_skipped urls-store || ok=0
    # systemd unit + restart always happen
    assert_decided systemd-unit || ok=0
    assert_decided service-restart || ok=0
    [ $ok -eq 1 ]
}

s03_c_source_newer_than_binary() {
    populate_fully_built
    # Drop a fake source file with mtime newer than the zet binary.
    # populate_fully_built creates the binary with "now" mtime so we
    # need a future date to be newer.
    mkdir -p "$SANDBOX/install/zettair/devel/src"
    touch -t 203006010000 "$SANDBOX/install/zettair/devel/src/okapi.c"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided zet-binary || ok=0
    # Index should NOT rebuild — sidecars present, TREC unchanged
    assert_skipped index || ok=0
    assert_skipped click-prior || ok=0
    [ $ok -eq 1 ]
}

s04_trec_newer_than_index() {
    populate_fully_built
    # TREC mtime is now > index files
    touch -t 202402010000 "$SANDBOX/volume/enwiki_top1m.trec"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided index || ok=0
    # Cascade: docstore rebuilds because TREC > docstore
    assert_decided docstore || ok=0
    [ $ok -eq 1 ]
}

s05_trec_newer_than_docstore_only() {
    populate_fully_built
    # Just the docstore is older than TREC, nothing else
    touch -t 202312010000 "$SANDBOX/volume/enwiki_top1m.docstore"
    OUTPUT=$(run_setup)
    local ok=1
    assert_skipped index || ok=0
    assert_decided docstore || ok=0
    [ $ok -eq 1 ]
}

s06_field_lengths_missing() {
    populate_fully_built
    rm -f "$SANDBOX/volume/wikiindex/index.field_lengths"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided index || ok=0
    # Index rebuild regenerates all sidecars
    [ $ok -eq 1 ]
}

s07_docno_map_newer_than_click_prior() {
    populate_fully_built
    # Bump docno_map past click_prior
    touch -t 202402010000 "$SANDBOX/volume/wikiindex/index.docno_map.tsv"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided click-prior || ok=0
    # but index itself shouldn't rebuild — TREC is older
    assert_skipped index || ok=0
    [ $ok -eq 1 ]
}

s08_new_clickstream_added() {
    populate_fully_built
    # A new clickstream file lands, newer than click_prior + autosuggest
    touch -t 202402010000 "$SANDBOX/install/zettair/wikipedia/clickstream-enwiki-2025-04.tsv.gz"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided click-prior || ok=0
    assert_decided autosuggest || ok=0
    assert_skipped index || ok=0
    assert_skipped docstore || ok=0
    [ $ok -eq 1 ]
}

s09_all_clickstream_old() {
    populate_fully_built
    # Touch everything to a future date so click_prior is newer than
    # all of its inputs — should skip.
    touch -t 202506010000 "$SANDBOX/volume/wikiindex/index.click_prior.bin"
    touch -t 202506010000 "$SANDBOX/volume/autosuggest.json"
    OUTPUT=$(run_setup)
    local ok=1
    assert_skipped click-prior || ok=0
    assert_skipped autosuggest || ok=0
    [ $ok -eq 1 ]
}

s10_legacy_index_no_sidecars() {
    populate_fully_built
    # Pretend an old index that pre-dates PRD-019 sidecars
    rm -f "$SANDBOX/volume/wikiindex/index.field_lengths" \
          "$SANDBOX/volume/wikiindex/index.field_stats" \
          "$SANDBOX/volume/wikiindex/index.docno_map.tsv"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided index || ok=0
    [ $ok -eq 1 ]
}

s11_systemd_unit_always_rsynced() {
    populate_fully_built
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided systemd-unit || ok=0
    assert_decided service-restart || ok=0
    [ $ok -eq 1 ]
}

s12_autosuggest_only() {
    populate_fully_built
    rm -f "$SANDBOX/volume/autosuggest.json"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided autosuggest || ok=0
    assert_skipped index || ok=0
    assert_skipped click-prior || ok=0
    assert_skipped docstore || ok=0
    [ $ok -eq 1 ]
}

s13_partial_index() {
    populate_fully_built
    # Simulate a half-built index (param.0 missing but vocab there)
    rm -f "$SANDBOX/volume/wikiindex/index.param.0"
    touch "$SANDBOX/volume/wikiindex/index.vocab.0"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided index || ok=0
    [ $ok -eq 1 ]
}

s14_full_cascade() {
    populate_fully_built
    # Everything stale — TREC newer than everything
    touch -t 202506010000 "$SANDBOX/volume/enwiki_top1m.trec"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided index || ok=0
    assert_decided docstore || ok=0
    # click_prior cascades because index is going to rebuild → new
    # docno_map → click_prior detects docno_map fresher (even though
    # we don't actually rebuild in dry-run, it's already detected
    # because we set TREC newer; index step would have produced
    # newer docno_map). In dry-run the docno_map mtime didn't
    # change, so this might still skip — both behaviours acceptable.
    [ $ok -eq 1 ]
}

s15_clickstream_removed() {
    populate_fully_built
    # Remove a clickstream file — staleness is one-way (newer inputs
    # trigger rebuild). Removing one doesn't.
    rm -f "$SANDBOX/install/zettair/wikipedia/clickstream-enwiki-2024-06.tsv.gz"
    OUTPUT=$(run_setup)
    local ok=1
    assert_skipped click-prior || ok=0
    assert_skipped autosuggest || ok=0
    [ $ok -eq 1 ]
}

s16_zet_binary_is_libtool_wrapper() {
    populate_fully_built
    # Replace the ELF-claiming `file` shim with one that reports the
    # binary as a shell script (libtool wrapper) — should trigger rebuild.
    cat > "$SANDBOX/shim/file" <<'EOF'
#!/usr/bin/env bash
echo "$1: Bourne-Again shell script text executable, ASCII text"
EOF
    chmod +x "$SANDBOX/shim/file"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided zet-binary || ok=0
    [ $ok -eq 1 ]
}

s17_clock_skew_future_dates() {
    populate_fully_built
    # All artefacts in the year 2030 — should still skip because
    # all inputs are older than outputs.
    find "$SANDBOX/volume" -type f -exec touch -t 203001010000 {} \;
    find "$SANDBOX/install/zettair/wikipedia" -name 'clickstream*' -exec touch -t 202501010000 {} \;
    OUTPUT=$(run_setup)
    local ok=1
    assert_skipped index || ok=0
    assert_skipped click-prior || ok=0
    assert_skipped docstore || ok=0
    [ $ok -eq 1 ]
}

s18_field_stats_missing_only() {
    populate_fully_built
    rm -f "$SANDBOX/volume/wikiindex/index.field_stats"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided index || ok=0
    [ $ok -eq 1 ]
}

s19_docno_map_missing_only() {
    populate_fully_built
    rm -f "$SANDBOX/volume/wikiindex/index.docno_map.tsv"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided index || ok=0
    [ $ok -eq 1 ]
}

s20_click_prior_missing() {
    populate_fully_built
    rm -f "$SANDBOX/volume/wikiindex/index.click_prior.bin"
    OUTPUT=$(run_setup)
    local ok=1
    assert_decided click-prior || ok=0
    assert_skipped index || ok=0
    [ $ok -eq 1 ]
}

# ── Run all scenarios ──────────────────────────────────────────────────────

mkdir -p "$SANDBOX_ROOT"

scenario "01 fresh box"                          s01_fresh_box
scenario "02 no-op (fully built)"                s02_no_op
scenario "03 C source newer than binary"         s03_c_source_newer_than_binary
scenario "04 TREC newer than index"              s04_trec_newer_than_index
scenario "05 TREC newer than docstore only"      s05_trec_newer_than_docstore_only
scenario "06 field_lengths sidecar deleted"      s06_field_lengths_missing
scenario "07 docno_map newer than click_prior"   s07_docno_map_newer_than_click_prior
scenario "08 new clickstream added"              s08_new_clickstream_added
scenario "09 outputs newer than all clickstream" s09_all_clickstream_old
scenario "10 legacy index (no sidecars)"         s10_legacy_index_no_sidecars
scenario "11 systemd unit always rsynced"        s11_systemd_unit_always_rsynced
scenario "12 autosuggest deleted"                s12_autosuggest_only
scenario "13 partial index (param.0 missing)"    s13_partial_index
scenario "14 full cascade (TREC newer)"          s14_full_cascade
scenario "15 clickstream file removed"           s15_clickstream_removed
scenario "16 zet binary is libtool wrapper"      s16_zet_binary_is_libtool_wrapper
scenario "17 clock skew (future dates)"          s17_clock_skew_future_dates
scenario "18 field_stats sidecar deleted"        s18_field_stats_missing_only
scenario "19 docno_map sidecar deleted"          s19_docno_map_missing_only
scenario "20 click_prior deleted"                s20_click_prior_missing

# ── Summary ─────────────────────────────────────────────────────────────────

TOTAL=$((PASS+FAIL))
echo "═════════════════════════════════════════════════"
if [ $FAIL -eq 0 ]; then
    echo "${GREEN}${BOLD}All $PASS/$TOTAL scenarios passed.${RESET}"
    exit 0
else
    echo "${RED}${BOLD}$FAIL/$TOTAL scenarios failed:${RESET}"
    for t in "${FAILED_TESTS[@]}"; do
        echo "  - $t"
    done
    exit 1
fi
