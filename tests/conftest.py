"""
Pytest configuration and fixtures
"""
import pytest
import os
import sys
from unittest.mock import Mock, MagicMock

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture
def mock_env(monkeypatch):
    """Mock environment variables"""
    env_vars = {
        'BOT_TOKENS': 'test_token_123',
        'SECRET_KEY': 'test_secret_key',
        'MONGO_URI': 'mongodb://localhost:27017/',
        'DB_NAME': 'test_bot_db',
        'CHUNK_DURATION_SEC': '40',
        'TRANSCRIBE_MAX_WORKERS': '2',
        'MAX_CONCURRENT_TRANSCRIPTS': '2',
    }
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    return env_vars


@pytest.fixture
def mock_mongo():
    """Mock MongoDB client"""
    mock_client = Mock()
    mock_db = Mock()
    mock_users = Mock()
    mock_groups = Mock()
    
    mock_db.__getitem__ = lambda self, key: mock_users if key == 'users' else mock_groups
    mock_client.__getitem__ = lambda self, key: mock_db
    
    mock_users.find_one.return_value = {'user_id': '123', 'stt_language': 'en'}
    mock_users.update_one.return_value = Mock()
    
    return mock_client


@pytest.fixture
def mock_bot():
    """Mock Telegram bot"""
    bot = Mock()
    bot.get_chat_member.return_value = Mock(status='member')
    bot.send_message.return_value = Mock()
    bot.get_chat.return_value = Mock(type='private')
    return bot


@pytest.fixture
def flask_app():
    """Flask test client"""
    # Import here to avoid circular imports
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    return app


@pytest.fixture
def flask_client(flask_app):
    """Flask test client"""
    return flask_app.test_client()

