"""CodeScope Language Server Protocol server implementation."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

from codescope.analyzers.orchestrator import AnalysisOrchestrator
from codescope.core.config import AnalysisConfig
from codescope.core.enums import Severity

logger = logging.getLogger(__name__)


class CodeScopeLSPServer:
    """Language Server Protocol server for CodeScope."""

    def __init__(self):
        self.workspace_folders: list[Path] = []
        self.config: Optional[AnalysisConfig] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._initialized = False
        self._shutdown_requested = False
        self._diagnostics: dict[str, list[dict]] = {}

    async def start(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Start the language server."""
        self._reader = reader
        self._writer = writer

        logger.info("CodeScope LSP Server starting...")

        while not self._shutdown_requested:
            try:
                message = await self._read_message()
                if message is None:
                    break
                await self._handle_message(message)
            except Exception as e:
                logger.error(f"Error handling message: {e}")

        logger.info("CodeScope LSP Server stopped")

    async def _read_message(self) -> Optional[dict]:
        """Read a JSON-RPC message from stdin."""
        if not self._reader:
            return None

        # Read headers
        headers: dict[str, str] = {}
        while True:
            line = await self._reader.readline()
            if not line:
                return None
            line = line.decode('utf-8').strip()
            if not line:
                break
            key, value = line.split(': ', 1)
            headers[key] = value

        # Read content
        content_length = int(headers.get('Content-Length', 0))
        if content_length == 0:
            return None

        content = await self._reader.read(content_length)
        return json.loads(content.decode('utf-8'))

    def _write_message(self, message: dict):
        """Write a JSON-RPC message to stdout."""
        if not self._writer:
            return

        content = json.dumps(message)
        content_bytes = content.encode('utf-8')

        header = f"Content-Length: {len(content_bytes)}\r\n\r\n"
        self._writer.write(header.encode('utf-8'))
        self._writer.write(content_bytes)

    async def _handle_message(self, message: dict):
        """Handle incoming JSON-RPC message."""
        method = message.get('method', '')
        msg_id = message.get('id')
        params = message.get('params', {})

        logger.debug(f"Received: {method}")

        # Requests (have id)
        if msg_id is not None:
            result = await self._handle_request(method, params)
            self._send_response(msg_id, result)
        # Notifications (no id)
        else:
            await self._handle_notification(method, params)

    async def _handle_request(self, method: str, params: dict) -> Any:
        """Handle JSON-RPC request."""
        if method == 'initialize':
            return self._handle_initialize(params)
        elif method == 'shutdown':
            return self._handle_shutdown()
        elif method == 'textDocument/codeAction':
            return self._handle_code_action(params)
        elif method == 'textDocument/hover':
            return self._handle_hover(params)
        elif method == 'codescope/analyze':
            return await self._handle_analyze(params)
        else:
            logger.warning(f"Unknown request method: {method}")
            return None

    async def _handle_notification(self, method: str, params: dict):
        """Handle JSON-RPC notification."""
        if method == 'initialized':
            self._initialized = True
            logger.info("LSP server initialized")
        elif method == 'exit':
            self._shutdown_requested = True
        elif method == 'textDocument/didOpen':
            await self._handle_did_open(params)
        elif method == 'textDocument/didSave':
            await self._handle_did_save(params)
        elif method == 'textDocument/didClose':
            self._handle_did_close(params)
        elif method == 'workspace/didChangeConfiguration':
            self._handle_config_change(params)
        else:
            logger.debug(f"Unhandled notification: {method}")

    def _send_response(self, msg_id: Any, result: Any):
        """Send JSON-RPC response."""
        self._write_message({
            'jsonrpc': '2.0',
            'id': msg_id,
            'result': result,
        })

    def _send_notification(self, method: str, params: dict):
        """Send JSON-RPC notification."""
        self._write_message({
            'jsonrpc': '2.0',
            'method': method,
            'params': params,
        })

    def _handle_initialize(self, params: dict) -> dict:
        """Handle initialize request."""
        # Extract workspace folders
        if 'workspaceFolders' in params:
            self.workspace_folders = [
                Path(folder['uri'].replace('file://', ''))
                for folder in params['workspaceFolders']
            ]
        elif 'rootPath' in params:
            self.workspace_folders = [Path(params['rootPath'])]

        return {
            'capabilities': {
                'textDocumentSync': {
                    'openClose': True,
                    'change': 1,  # Full sync
                    'save': {'includeText': True},
                },
                'codeActionProvider': True,
                'hoverProvider': True,
                'diagnosticProvider': {
                    'interFileDependencies': False,
                    'workspaceDiagnostics': False,
                },
                'executeCommandProvider': {
                    'commands': [
                        'codescope.analyzeFile',
                        'codescope.analyzeWorkspace',
                    ],
                },
            },
            'serverInfo': {
                'name': 'CodeScope LSP',
                'version': '0.1.0',
            },
        }

    def _handle_shutdown(self) -> None:
        """Handle shutdown request."""
        self._shutdown_requested = True
        return None

    async def _handle_did_open(self, params: dict):
        """Handle textDocument/didOpen notification."""
        uri = params['textDocument']['uri']
        await self._analyze_document(uri)

    async def _handle_did_save(self, params: dict):
        """Handle textDocument/didSave notification."""
        uri = params['textDocument']['uri']
        await self._analyze_document(uri)

    def _handle_did_close(self, params: dict):
        """Handle textDocument/didClose notification."""
        uri = params['textDocument']['uri']
        # Clear diagnostics for closed file
        self._publish_diagnostics(uri, [])

    def _handle_config_change(self, params: dict):
        """Handle workspace/didChangeConfiguration notification."""
        settings = params.get('settings', {}).get('codescope', {})
        logger.info(f"Configuration changed: {settings}")

    def _handle_code_action(self, params: dict) -> list[dict]:
        """Handle textDocument/codeAction request."""
        uri = params['textDocument']['uri']
        diagnostics = params.get('context', {}).get('diagnostics', [])

        actions = []

        for diagnostic in diagnostics:
            if diagnostic.get('source') != 'codescope':
                continue

            rule_id = diagnostic.get('code', '')

            # Learn more action
            actions.append({
                'title': f'Learn more about {rule_id}',
                'kind': 'quickfix',
                'command': {
                    'title': 'Open documentation',
                    'command': 'vscode.open',
                    'arguments': [f'https://codescope.dev/rules/{rule_id}'],
                },
            })

            # Suppress action
            actions.append({
                'title': f'Suppress {rule_id} for this line',
                'kind': 'quickfix',
                'edit': {
                    'changes': {
                        uri: [
                            {
                                'range': {
                                    'start': diagnostic['range']['start'],
                                    'end': diagnostic['range']['start'],
                                },
                                'newText': f'# codescope-disable-next-line {rule_id}\n',
                            }
                        ]
                    }
                },
            })

        return actions

    def _handle_hover(self, params: dict) -> Optional[dict]:
        """Handle textDocument/hover request."""
        uri = params['textDocument']['uri']
        position = params['position']

        # Check if there's a diagnostic at this position
        diagnostics = self._diagnostics.get(uri, [])
        for diag in diagnostics:
            if self._position_in_range(position, diag['range']):
                return {
                    'contents': {
                        'kind': 'markdown',
                        'value': f"**{diag.get('code', 'Issue')}**\n\n{diag['message']}",
                    },
                }

        return None

    async def _handle_analyze(self, params: dict) -> dict:
        """Handle codescope/analyze custom request."""
        uri = params.get('uri', '')
        await self._analyze_document(uri)
        return {'status': 'ok'}

    async def _analyze_document(self, uri: str):
        """Analyze a document and publish diagnostics."""
        file_path = Path(uri.replace('file://', ''))

        if not file_path.exists():
            return

        try:
            config = AnalysisConfig(project_path=file_path.parent)
            orchestrator = AnalysisOrchestrator(config)

            # Analyze single file
            results = orchestrator.analyze_file(file_path)

            # Convert to diagnostics
            diagnostics = []
            for issue in results.issues:
                diagnostics.append({
                    'range': {
                        'start': {
                            'line': max(0, issue.location.start_line - 1),
                            'character': issue.location.start_column or 0,
                        },
                        'end': {
                            'line': max(0, issue.location.end_line - 1),
                            'character': issue.location.end_column or 1000,
                        },
                    },
                    'severity': self._severity_to_lsp(issue.severity),
                    'code': issue.rule_id,
                    'source': 'codescope',
                    'message': issue.message,
                })

            self._diagnostics[uri] = diagnostics
            self._publish_diagnostics(uri, diagnostics)

        except Exception as e:
            logger.error(f"Analysis failed: {e}")

    def _publish_diagnostics(self, uri: str, diagnostics: list[dict]):
        """Publish diagnostics to the client."""
        self._send_notification('textDocument/publishDiagnostics', {
            'uri': uri,
            'diagnostics': diagnostics,
        })

    def _severity_to_lsp(self, severity: Severity) -> int:
        """Convert CodeScope severity to LSP severity."""
        mapping = {
            Severity.BLOCKER: 1,   # Error
            Severity.CRITICAL: 1,  # Error
            Severity.MAJOR: 2,     # Warning
            Severity.MINOR: 3,     # Information
            Severity.INFO: 4,      # Hint
        }
        return mapping.get(severity, 2)

    def _position_in_range(self, position: dict, range_: dict) -> bool:
        """Check if position is within range."""
        start = range_['start']
        end = range_['end']

        if position['line'] < start['line'] or position['line'] > end['line']:
            return False

        if position['line'] == start['line'] and position['character'] < start['character']:
            return False

        if position['line'] == end['line'] and position['character'] > end['character']:
            return False

        return True


async def main():
    """Main entry point for LSP server."""
    logging.basicConfig(level=logging.INFO)

    server = CodeScopeLSPServer()

    reader, writer = await asyncio.open_connection(
        limit=10 * 1024 * 1024  # 10MB limit
    )

    await server.start(
        asyncio.StreamReader(),
        asyncio.StreamWriter(writer, None, reader, None)
    )


if __name__ == '__main__':
    asyncio.run(main())
