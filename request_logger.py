#!/usr/bin/env python3
"""
Request logger for logging request/response bodies with rotation.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional
import asyncio
from collections import deque


class RequestLogger:
    """Logger that keeps only the last N requests in a file."""

    def __init__(self, log_file: str = "requests.log", max_entries: int = 100):
        self.log_file = Path(log_file)
        self.max_entries = max_entries
        self.lock = asyncio.Lock()

    async def log_request(
        self,
        request_body: Optional[str | bytes],
        response_body: Optional[str | bytes],
        status_code: int,
        path: str = "",
    ):
        """Log a request/response pair."""
        async with self.lock:
            # Read existing entries
            entries = deque(maxlen=self.max_entries)
            if self.log_file.exists():
                try:
                    with open(self.log_file, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    entries.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                except Exception:
                    pass

            # Prepare new entry
            entry = {
                "timestamp": datetime.now().isoformat(),
                "path": path,
                "status": status_code,
                "request": self._to_str(request_body),
                "response": self._to_str(response_body),
            }

            # Add new entry (deque will automatically remove oldest if > max_entries)
            entries.append(entry)

            # Write all entries back
            with open(self.log_file, 'w', encoding='utf-8') as f:
                for e in entries:
                    f.write(json.dumps(e, ensure_ascii=False) + '\n')

    def _to_str(self, data: Optional[str | bytes]) -> str:
        """Convert data to string for logging."""
        if data is None:
            return ""
        if isinstance(data, bytes):
            try:
                return data.decode('utf-8')
            except UnicodeDecodeError:
                return data.decode('utf-8', errors='ignore')
        return str(data)
