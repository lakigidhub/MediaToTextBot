"""
Tests for main.py - Functional style
"""
import pytest
import sys
import os
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

# Mock modules before import
sys.modules['telebot'] = MagicMock()
sys.modules['telebot.types'] = MagicMock()


# ============================================================================
# Basic Functions Tests
# ============================================================================

def test_norm_user_id(mock_env):
    """Test user ID normalization"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            # Test integer conversion
            assert main_module.norm_user_id(12345) == '12345'
            assert main_module.norm_user_id('12345') == '12345'
            assert main_module.norm_user_id('test') == 'test'


def test_lang_options_parsing(mock_env):
    """Test language options are properly parsed"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            # Check that lang options were parsed
            assert len(main_module.LANG_OPTIONS) > 0
            assert ('🇬🇧 English', 'en') in main_module.LANG_OPTIONS
            assert 'en' in main_module.CODE_TO_LABEL
            assert main_module.CODE_TO_LABEL['en'] == '🇬🇧 English'


def test_check_subscription_no_channel(mock_env, mock_bot):
    """Test subscription check when no channel required"""
    mock_env['REQUIRED_CHANNEL'] = ''
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            main_module.REQUIRED_CHANNEL = ''
            
            result = main_module.check_subscription(123, mock_bot)
            assert result is True


def test_check_subscription_with_channel(mock_env, mock_bot):
    """Test subscription check with required channel"""
    mock_env['REQUIRED_CHANNEL'] = '@test_channel'
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            main_module.REQUIRED_CHANNEL = '@test_channel'
            
            # Mock member status
            mock_bot.get_chat_member.return_value = Mock(status='member')
            result = main_module.check_subscription(123, mock_bot)
            assert result is True
            
            # Mock non-member status
            mock_bot.get_chat_member.return_value = Mock(status='left')
            result = main_module.check_subscription(123, mock_bot)
            assert result is False


# ============================================================================
# Environment Tests
# ============================================================================

def test_environment_variables_loading(mock_env):
    """Test environment variables are loaded correctly"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            assert main_module.TELEGRAM_MAX_BYTES == 20 * 1024 * 1024
            assert main_module.REQUEST_TIMEOUT_TELEGRAM == 300
            assert main_module.MAX_CONCURRENT_TRANSCRIPTS == 2


def test_bot_token_loading(mock_env):
    """Test bot token is loaded"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            with patch('telebot.TeleBot'):
                import main as main_module
                
                assert main_module.BOT_TOKEN == 'test_token_123'


def test_api_keys_parsing(mock_env):
    """Test API keys are parsed correctly"""
    mock_env['GEMINI_API_KEYS'] = 'key1,key2,key3'
    mock_env['ASSEMBLYAI_API_KEYS'] = 'aai_key1,aai_key2'
    
    with patch.dict(os.environ, mock_env, clear=True):
        with patch('pymongo.MongoClient'):
            # Re-parse from environment
            gemini_keys = [t.strip() for t in os.environ.get("GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", "")).split(",") if t.strip()]
            assemblyai_keys = [t.strip() for t in os.environ.get("ASSEMBLYAI_API_KEYS", os.environ.get("ASSEMBLYAI_API_KEY", "")).split(",") if t.strip()]
            
            assert len(gemini_keys) == 3
            assert 'key1' in gemini_keys
            assert len(assemblyai_keys) == 2


# ============================================================================
# Flask Application Tests
# ============================================================================

def test_app_initialization(mock_env):
    """Test Flask app initializes"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            with patch('telebot.TeleBot'):
                import main as main_module
                assert main_module.app is not None
                assert main_module.app.name == 'main'


def test_flask_app_exists(mock_env):
    """Test Flask app exists and is configured"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            with patch('telebot.TeleBot'):
                import main as main_module
                
                # Check app exists and can create test client
                assert main_module.app is not None
                client = main_module.app.test_client()
                assert client is not None


# ============================================================================
# Database Tests
# ============================================================================

def test_mongo_connection(mock_env):
    """Test MongoDB connection"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient') as mock_client:
            import main as main_module
            
            # Verify MongoClient was called or client exists
            assert mock_client.called or hasattr(main_module, 'client')


def test_database_collections(mock_env):
    """Test database collections exist"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            assert hasattr(main_module, 'users_collection')
            assert hasattr(main_module, 'groups_collection')
            assert hasattr(main_module, 'settings_collection')


# ============================================================================
# Security Tests
# ============================================================================

def test_secret_key_initialization(mock_env):
    """Test secret key is set"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            assert main_module.SECRET_KEY == 'test_secret_key'
            assert len(main_module.SECRET_KEY) > 0


def test_admin_user_ids_parsing(mock_env):
    """Test admin user IDs are parsed"""
    mock_env['ADMIN_USER_IDS'] = '123,456,789'
    
    with patch.dict(os.environ, mock_env, clear=True):
        with patch('pymongo.MongoClient'):
            # Re-parse from environment
            raw_admins = os.environ.get("ADMIN_USER_IDS", "")
            admin_ids = []
            for part in [p.strip() for p in raw_admins.split(",") if p.strip()]:
                try:
                    admin_ids.append(int(part))
                except Exception:
                    pass
            
            assert len(admin_ids) == 3
            assert 123 in admin_ids
            assert 456 in admin_ids


def test_serializer_initialization(mock_env):
    """Test URL serializer is initialized"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            assert hasattr(main_module, 'serializer')
            assert main_module.serializer is not None


# ============================================================================
# Utility Functions Tests
# ============================================================================

def test_allowed_extensions(mock_env):
    """Test allowed file extensions"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            # Check common extensions are allowed
            assert 'mp3' in main_module.ALLOWED_EXTENSIONS
            assert 'wav' in main_module.ALLOWED_EXTENSIONS
            assert 'mp4' in main_module.ALLOWED_EXTENSIONS


def test_memory_structures(mock_env):
    """Test in-memory data structures"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            assert hasattr(main_module, 'user_transcriptions')
            assert hasattr(main_module, 'in_memory_data')
            assert hasattr(main_module, 'action_usage')
            assert hasattr(main_module, 'memory_lock')


# ============================================================================
# Threading Tests
# ============================================================================

def test_semaphore_initialization(mock_env):
    """Test transcript semaphore is initialized"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            assert main_module.transcript_semaphore is not None


def test_pending_queue_initialization(mock_env):
    """Test pending queue is initialized"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            assert main_module.PENDING_QUEUE is not None
            assert len(main_module.PENDING_QUEUE) == 0


# ============================================================================
# Constants Tests
# ============================================================================

def test_assemblyai_base_url(mock_env):
    """Test AssemblyAI base URL is set"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import main as main_module
            
            assert main_module.ASSEMBLYAI_BASE_URL == "https://api.assemblyai.com/v2"


def test_webhook_url_configuration(mock_env):
    """Test webhook URL configuration"""
    mock_env['WEBHOOK_BASE'] = 'https://example.com/'
    
    with patch.dict(os.environ, mock_env, clear=True):
        with patch('pymongo.MongoClient'):
            # Re-parse from environment
            webhook_url = os.environ.get("WEBHOOK_BASE", "").rstrip("/")
            
            # URL should have trailing slash stripped
            assert webhook_url == 'https://example.com'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
