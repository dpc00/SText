"""
ai_search_app.py -- Search Claude Code conversation history.

Launch via: python ai_search_app.py
Then open:  http://127.0.0.1:5758
Or use the Sublime command: Ai: Search Conversations
"""

import json
import re
from datetime import datetime
from pathlib import Path

try:
    from flask import Flask, render_template_string, request
    app = Flask(__name__)
except ImportError:
    Flask = render_template_string = request = None
    class _Stub:
        def route(self, *a, **k): return lambda f: f
        jinja_env = type('', (), {'filters': {}})()
    app = _Stub()

PROJECTS_DIR = Path.home() / ".claude" / "projects"
PORT = 5758


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def decode_project(folder_name: str) -> str:
    return re.sub(r'^[A-Z]--Users-[^-]+-', '', folder_name)


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(
            b.get('text', '') for b in content
            if isinstance(b, dict) and b.get('type') == 'text'
        )
    return ''


def fmt_ts(ts):
    if not ts:
        return ''
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ts[:16]


def get_projects():
    if not PROJECTS_DIR.exists():
        return []
    return sorted(
        [(d.name, decode_project(d.name)) for d in PROJECTS_DIR.iterdir() if d.is_dir()],
        key=lambda x: x[1]
    )


def read_session(jsonl_path: Path) -> dict:
    title = None
    turns = []
    pending_user = None
    first_ts = last_ts = None

    try:
        with open(jsonl_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = obj.get('timestamp')
                if ts:
                    if not first_ts:
                        first_ts = ts
                    last_ts = ts

                t = obj.get('type')

                if not title and t == 'ai-title':
                    title = obj.get('aiTitle', '')

                elif t == 'user' and not obj.get('isMeta'):
                    text = extract_text(obj.get('message', {}).get('content', ''))
                    if text and not text.startswith('<'):
                        pending_user = {'user_text': text, 'user_ts': ts, 'assistant_text': '', 'assistant_ts': None}

                elif t == 'assistant' and pending_user:
                    text = extract_text(obj.get('message', {}).get('content', []))
                    if text:
                        pending_user['assistant_text'] = text
                        pending_user['assistant_ts'] = ts
                        turns.append(pending_user)
                        pending_user = None

    except OSError:
        pass

    # flush any pending user message with no assistant reply yet
    if pending_user:
        turns.append(pending_user)

    return {'title': title or '(untitled)', 'turns': turns, 'first_ts': first_ts, 'last_ts': last_ts}


def do_search(keywords_str, project_filter, date_from_str, date_to_str, search_in, title_filter):
    keywords = [k.strip().lower() for k in keywords_str.split() if k.strip()] if keywords_str else []

    def parse_date(s):
        try:
            return datetime.strptime(s, '%Y-%m-%d').date() if s else None
        except ValueError:
            return None

    date_from = parse_date(date_from_str)
    date_to   = parse_date(date_to_str)

    results = []

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        if project_filter and project_filter != project_dir.name:
            continue

        project_display = decode_project(project_dir.name)

        for jsonl in sorted(project_dir.glob('*.jsonl'), key=lambda f: f.stat().st_mtime, reverse=True):
            if jsonl.parent != project_dir:
                continue

            mtime_date = datetime.fromtimestamp(jsonl.stat().st_mtime).date()
            if date_from and mtime_date < date_from:
                continue
            if date_to and mtime_date > date_to:
                continue

            session = read_session(jsonl)

            if title_filter and title_filter.lower() not in session['title'].lower():
                continue

            # Session-level match: all keywords must appear somewhere in the session
            if keywords:
                session_text = ''
                for turn in session['turns']:
                    if search_in in ('user', 'both'):
                        session_text += turn['user_text'].lower() + ' '
                    if search_in in ('assistant', 'both'):
                        session_text += turn['assistant_text'].lower() + ' '
                if not all(kw in session_text for kw in keywords):
                    continue

            # Show turns containing any keyword
            matching_turns = []
            for turn in session['turns']:
                haystack = ''
                if search_in in ('user', 'both'):
                    haystack += turn['user_text'].lower() + ' '
                if search_in in ('assistant', 'both'):
                    haystack += turn['assistant_text'].lower()

                if not keywords or any(kw in haystack for kw in keywords):
                    matching_turns.append(turn)

            if matching_turns:
                results.append({
                    'title': session['title'],
                    'project': project_display,
                    'file': str(jsonl),
                    'mtime': mtime_date.isoformat(),
                    'first_ts': fmt_ts(session['first_ts']),
                    'last_ts': fmt_ts(session['last_ts']),
                    'total_turns': len(session['turns']),
                    'matching_turns': matching_turns,
                })

    return results


def highlight(text, keywords):
    for kw in keywords:
        text = re.sub(f'({re.escape(kw)})', r'<mark class="bg-warning">\1</mark>', text, flags=re.IGNORECASE)
    return text


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Claude Conversation Search</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background: #f8f9fa; }
    .search-panel { background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.1); }
    .result-card { background: white; border-radius: 8px; margin-bottom: 1.2rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden; }
    .result-header { background: #343a40; color: white; padding: .6rem 1rem; font-size: .9rem; }
    .result-header .project { color: #adb5bd; }
    .session-link:hover { text-decoration: underline !important; }
    .turn { border-top: 1px solid #e9ecef; padding: .8rem 1rem; }
    .turn:first-child { border-top: none; }
    .turn-user { background: #f0f4ff; border-left: 3px solid #6c8ebf; padding: .5rem .75rem; border-radius: 4px; margin-bottom: .5rem; font-size: .88rem; white-space: pre-wrap; word-break: break-word; }
    .turn-assistant { background: #f6fff6; border-left: 3px solid #5a9e6f; padding: .5rem .75rem; border-radius: 4px; font-size: .88rem; white-space: pre-wrap; word-break: break-word; }
    .label-u { font-size: .7rem; font-weight: 700; color: #6c8ebf; text-transform: uppercase; letter-spacing: .05em; margin-bottom: .2rem; }
    .label-a { font-size: .7rem; font-weight: 700; color: #5a9e6f; text-transform: uppercase; letter-spacing: .05em; margin-bottom: .2rem; }
    .ts { font-size: .75rem; color: #6c757d; }
    .more-turns { font-size: .8rem; color: #6c757d; padding: .4rem 1rem; border-top: 1px solid #e9ecef; }
    mark.bg-warning { padding: 0 2px; border-radius: 2px; }
  </style>
</head>
<body>
<div class="container py-4">

  <h4 class="mb-3">&#128269; Claude Conversation Search</h4>

  <div class="search-panel mb-4">
    <form method="get" action="/">
      <div class="row g-3">

        <div class="col-md-6">
          <label class="form-label fw-semibold">Keywords</label>
          <input type="text" class="form-control" name="q" value="{{ q }}"
                 placeholder="e.g. gmail mcp missing" autofocus>
          <div class="form-text">All words must appear in the same exchange (AND logic)</div>
        </div>

        <div class="col-md-6">
          <label class="form-label fw-semibold">Session title contains</label>
          <input type="text" class="form-control" name="title" value="{{ title_filter }}"
                 placeholder="e.g. MCP server">
        </div>

        <div class="col-md-3">
          <label class="form-label fw-semibold">Date from</label>
          <input type="date" class="form-control" name="from" value="{{ date_from }}">
        </div>

        <div class="col-md-3">
          <label class="form-label fw-semibold">Date to</label>
          <input type="date" class="form-control" name="to" value="{{ date_to }}">
        </div>

        <div class="col-md-3">
          <label class="form-label fw-semibold">Project</label>
          <select class="form-select" name="project">
            <option value="">All projects</option>
            {% for enc, disp in projects %}
            <option value="{{ enc }}" {% if enc == project_filter %}selected{% endif %}>{{ disp }}</option>
            {% endfor %}
          </select>
        </div>

        <div class="col-md-3">
          <label class="form-label fw-semibold">Search in</label>
          <select class="form-select" name="in">
            <option value="both"      {% if search_in == 'both'      %}selected{% endif %}>Both</option>
            <option value="assistant" {% if search_in == 'assistant' %}selected{% endif %}>Claude said</option>
            <option value="user"      {% if search_in == 'user'      %}selected{% endif %}>I said</option>
          </select>
        </div>

        <div class="col-12 d-flex gap-2 align-items-center">
          <button type="submit" class="btn btn-primary">Search</button>
          <a href="/" class="btn btn-outline-secondary">Clear</a>
          {% if searched %}
          <span class="text-muted ms-2">
            {{ total_matches }} match{{ 'es' if total_matches != 1 else '' }}
            in {{ total_sessions }} session{{ 's' if total_sessions != 1 else '' }}
            &mdash; {{ elapsed_ms }}ms
          </span>
          {% endif %}
        </div>

      </div>
    </form>
  </div>

  {% if searched and not results %}
  <div class="alert alert-secondary">No results found.</div>
  {% endif %}

  {% for r in results %}
  <div class="result-card">
    <div class="result-header d-flex justify-content-between align-items-start flex-wrap gap-1">
      <div>
        <a href="/session?file={{ r.file | urlencode }}&q={{ q | urlencode }}" class="text-white fw-semibold text-decoration-none session-link">{{ r.title }}</a>
        <span class="project ms-2">{{ r.project }}</span>
      </div>
      <div class="text-end">
        <span class="ts">{{ r.first_ts }}{% if r.last_ts and r.last_ts != r.first_ts %} &rarr; {{ r.last_ts }}{% endif %}</span>
        <span class="badge bg-secondary ms-2">{{ r.total_turns }} exchanges</span>
        <span class="badge bg-primary ms-1">{{ r.matching_turns|length }} matching</span>
      </div>
    </div>

    {% for turn in r.matching_turns[:3] %}
    <div class="turn">
      {% if turn.user_text %}
      <div class="label-u">You said{% if turn.user_ts %} &middot; <span class="ts">{{ turn.user_ts[:16].replace('T',' ') }}</span>{% endif %}</div>
      <div class="turn-user">{{ highlight(turn.user_text[:600], keywords) | safe }}{% if turn.user_text|length > 600 %}<span class="text-muted"> …</span>{% endif %}</div>
      {% endif %}
      {% if turn.assistant_text %}
      <div class="label-a mt-2">Claude said{% if turn.assistant_ts %} &middot; <span class="ts">{{ turn.assistant_ts[:16].replace('T',' ') }}</span>{% endif %}</div>
      <div class="turn-assistant">{{ highlight(turn.assistant_text[:1200], keywords) | safe }}{% if turn.assistant_text|length > 1200 %}<span class="text-muted"> … <a href="/session?file={{ r.file | urlencode }}&q={{ q | urlencode }}">read full session &rarr;</a></span>{% endif %}</div>
      {% endif %}
    </div>
    {% endfor %}

    {% if r.matching_turns|length > 3 %}
    <div class="more-turns">
      + {{ r.matching_turns|length - 3 }} more matching exchange{{ 's' if r.matching_turns|length - 3 != 1 else '' }} &mdash;
      <a href="/session?file={{ r.file | urlencode }}&q={{ q | urlencode }}">read full session &rarr;</a>
    </div>
    {% endif %}
  </div>
  {% endfor %}

</div>

<script>
function expand(btn) {
  const box = btn.previousElementSibling;
  box.classList.remove('collapsed-text');
  btn.remove();
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    projects = get_projects()

    q             = request.args.get('q', '').strip()
    title_filter  = request.args.get('title', '').strip()
    date_from     = request.args.get('from', '').strip()
    date_to       = request.args.get('to', '').strip()
    project_filter= request.args.get('project', '').strip()
    search_in     = request.args.get('in', 'both')

    searched = bool(q or title_filter or date_from or date_to or project_filter)
    results = []
    total_matches = total_sessions = elapsed_ms = 0
    keywords = [k.strip().lower() for k in q.split() if k.strip()] if q else []

    if searched:
        import time
        t0 = time.time()
        results = do_search(q, project_filter, date_from, date_to, search_in, title_filter)
        elapsed_ms = round((time.time() - t0) * 1000)
        total_sessions = len(results)
        total_matches = sum(len(r['matching_turns']) for r in results)

    return render_template_string(
        TEMPLATE,
        projects=projects,
        q=q,
        title_filter=title_filter,
        date_from=date_from,
        date_to=date_to,
        project_filter=project_filter,
        search_in=search_in,
        searched=searched,
        results=results,
        total_matches=total_matches,
        total_sessions=total_sessions,
        elapsed_ms=elapsed_ms,
        keywords=keywords,
        highlight=highlight,
    )


SESSION_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ session.title }}</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background: #f8f9fa; }
    .session-header { background: #343a40; color: white; padding: 1rem 1.5rem; border-radius: 8px; margin-bottom: 1.2rem; }
    .session-header .project { color: #adb5bd; font-size: .9rem; }
    .turn { background: white; border-radius: 8px; margin-bottom: .8rem; box-shadow: 0 1px 3px rgba(0,0,0,.07); overflow: hidden; }
    .turn-user { background: #f0f4ff; border-left: 4px solid #6c8ebf; padding: .75rem 1rem; font-size: .9rem; white-space: pre-wrap; word-break: break-word; }
    .turn-assistant { background: #f6fff6; border-left: 4px solid #5a9e6f; padding: .75rem 1rem; font-size: .9rem; white-space: pre-wrap; word-break: break-word; }
    .label-u { font-size: .7rem; font-weight: 700; color: #6c8ebf; text-transform: uppercase; letter-spacing: .05em; padding: .4rem 1rem .1rem; }
    .label-a { font-size: .7rem; font-weight: 700; color: #5a9e6f; text-transform: uppercase; letter-spacing: .05em; padding: .4rem 1rem .1rem; }
    .ts { font-size: .75rem; color: #6c757d; font-weight: normal; text-transform: none; letter-spacing: 0; }
    mark.bg-warning { padding: 0 2px; border-radius: 2px; }
    .highlight-turn { outline: 2px solid #ffc107; }
  </style>
</head>
<body>
<div class="container py-4" style="max-width:860px">

  <div class="mb-3">
    <a href="{{ back_url }}" class="btn btn-sm btn-outline-secondary">&larr; Back to results</a>
  </div>

  <div class="session-header">
    <div class="fw-semibold fs-5">{{ session.title }}</div>
    <div class="project mt-1">{{ project }} &mdash; {{ session.first_ts }} {% if session.last_ts and session.last_ts != session.first_ts %}&rarr; {{ session.last_ts }}{% endif %}</div>
    <div class="mt-1" style="font-size:.8rem;color:#adb5bd;">{{ session.turns|length }} exchanges</div>
  </div>

  {% for turn in session.turns %}
  <div class="turn {% if turn in matching_turns %}highlight-turn{% endif %}" id="turn-{{ loop.index0 }}">
    {% if turn.user_text %}
    <div class="label-u">You said {% if turn.user_ts %}<span class="ts">&middot; {{ turn.user_ts[:16].replace('T',' ') }}</span>{% endif %}</div>
    <div class="turn-user">{{ highlight(turn.user_text, keywords) | safe }}</div>
    {% endif %}
    {% if turn.assistant_text %}
    <div class="label-a">Claude said {% if turn.assistant_ts %}<span class="ts">&middot; {{ turn.assistant_ts[:16].replace('T',' ') }}</span>{% endif %}</div>
    <div class="turn-assistant">{{ highlight(turn.assistant_text, keywords) | safe }}</div>
    {% endif %}
  </div>
  {% endfor %}

</div>
<script>
  // Scroll to first highlighted turn
  const first = document.querySelector('.highlight-turn');
  if (first) first.scrollIntoView({behavior: 'smooth', block: 'start'});
</script>
</body>
</html>
"""


@app.route('/session')
def session_view():
    from urllib.parse import quote
    file_path = request.args.get('file', '')
    q = request.args.get('q', '')
    keywords = [k.strip().lower() for k in q.split() if k.strip()] if q else []

    if not file_path or not Path(file_path).exists():
        return "Session file not found.", 404

    session = read_session(Path(file_path))
    session['first_ts'] = fmt_ts(session['first_ts'])
    session['last_ts']  = fmt_ts(session['last_ts'])

    project = decode_project(Path(file_path).parent.name)

    # Which turns match (for highlighting outline)
    matching_turns = []
    for turn in session['turns']:
        haystack = (turn['user_text'] + ' ' + turn['assistant_text']).lower()
        if not keywords or any(kw in haystack for kw in keywords):
            matching_turns.append(turn)

    back_url = f'/?q={quote(q)}' if q else '/'

    return render_template_string(
        SESSION_TEMPLATE,
        session=session,
        project=project,
        keywords=keywords,
        matching_turns=matching_turns,
        back_url=back_url,
        highlight=highlight,
    )


if Flask is not None:
    from urllib.parse import quote as _quote
    app.jinja_env.filters['urlencode'] = lambda s: _quote(str(s), safe='')


if __name__ == '__main__':
    import webbrowser, threading
    url = f'http://127.0.0.1:{PORT}'
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f'Claude Search running at {url}')
    app.run(port=PORT, debug=False)
