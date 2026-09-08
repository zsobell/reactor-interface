"""The served pages and their module imports load without a frontend build."""
import asyncio
import re
import sys
from urllib.parse import urljoin

from reactor.server.app import create_app
from tests._support import Checker, request


async def main():
    c = Checker("test_static_assets")
    app = create_app()
    try:
        pending = ["/", "/analysis"]
        visited = set()
        while pending:
            url = pending.pop()
            if url in visited:
                continue
            visited.add(url)
            status, body = await request(app, url)
            c.check(f"asset loads: {url}", status == 200)
            if status != 200:
                continue
            text = body.decode()
            if url.endswith(".js"):
                refs = re.findall(r'from\s+[\'"]([^\'"]+)[\'"]', text)
            elif url.endswith(".css"):
                refs = []
            else:
                refs = re.findall(r'(?:src|href)="(/static/[^\"]+)"', text)
                c.check("page loads JavaScript as modules", 'type="module"' in text)
            pending.extend(urljoin(url, ref) for ref in refs)
        c.check("live chart module is reachable from the page",
                "/static/live-charts.js" in visited)
        c.check("analysis and control assets are separate",
                {"/static/analysis.js", "/static/control.js", "/static/analysis.css",
                 "/static/control.css"}.issubset(visited))
    finally:
        await app.state.supervisor.recording.close()
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
