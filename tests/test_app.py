"""
Tests for app.py - Functional style
"""
import pytest
import sys
import os
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

# Mock modules before import
sys.modules['telebot'] = MagicMock()
sys.modules['telebot.types'] = MagicMock()
sys.modules['speech_recognition'] = MagicMock()


# ============================================================================
# Basic Functions Tests
# ============================================================================

def test_norm_user_id(mock_env):
    """Test user ID normalization"""
    with patch.dict(os.environ, mock_env):
        import app as app_module
        
        # Test integer conversion
        assert app_module.norm_user_id(12345) == '12345'
        assert app_module.norm_user_id('12345') == '12345'
        assert app_module.norm_user_id('test') == 'test'


def test_lang_options_parsing(mock_env):
    """Test language options are properly parsed"""
    with patch.dict(os.environ, mock_env):
        import app as app_module
        
        # Check that lang options were parsed
        assert len(app_module.LANG_OPTIONS) > 0
        assert ('🇬🇧 English', 'en') in app_module.LANG_OPTIONS
        assert 'en' in app_module.CODE_TO_LABEL
        assert app_module.CODE_TO_LABEL['en'] == '🇬🇧 English'


def test_check_subscription_no_channel(mock_env, mock_bot):
    """Test subscription check when no channel required"""
    mock_env['REQUIRED_CHANNEL'] = ''
    with patch.dict(os.environ, mock_env):
        import app as app_module
        app_module.REQUIRED_CHANNEL = ''
        
        result = app_module.check_subscription(123, mock_bot)
        assert result is True


def test_check_subscription_with_channel(mock_env, mock_bot):
    """Test subscription check with required channel"""
    mock_env['REQUIRED_CHANNEL'] = '@test_channel'
    with patch.dict(os.environ, mock_env):
        import app as app_module
        app_module.REQUIRED_CHANNEL = '@test_channel'
        
        # Mock member status
        mock_bot.get_chat_member.return_value = Mock(status='member')
        result = app_module.check_subscription(123, mock_bot)
        assert result is True
        
        # Mock non-member status
        mock_bot.get_chat_member.return_value = Mock(status='left')
        result = app_module.check_subscription(123, mock_bot)
        assert result is False


def test_environment_variables_loading(mock_env):
    """Test environment variables are loaded correctly"""
    with patch.dict(os.environ, mock_env):
        import app as app_module
        
        assert app_module.CHUNK_DURATION_SEC == 40
        assert app_module.TRANSCRIBE_MAX_WORKERS == 2
        assert app_module.MAX_CONCURRENT_TRANSCRIPTS == 2


# ============================================================================
# Flask Application Tests
# ============================================================================

def test_app_initialization(mock_env):
    """Test Flask app initializes"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import app as app_module
            assert app_module.app is not None
            assert app_module.app.name == 'app'


def test_flask_app_exists(mock_env):
    """Test Flask app exists and is configured"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import app as app_module
            
            # Check app exists and can create test client
            assert app_module.app is not None
            client = app_module.app.test_client()
            assert client is not None


# ============================================================================
# Database Tests
# ============================================================================

def test_update_user_activity(mock_env):
    """Test user activity update"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient') as mock_client:
            mock_db = Mock()
            mock_users = Mock()
            mock_client.return_value = {mock_env['DB_NAME']: {'users': mock_users}}
            
            import app as app_module
            app_module.users_collection = mock_users
            
            app_module.update_user_activity(123)
            
            # Verify update_one was called
            assert mock_users.update_one.called


def test_get_stt_user_lang_default(mock_env):
    """Test getting user language with default"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import app as app_module
            app_module.users_collection = Mock()
            app_module.users_collection.find_one.return_value = None
            
            lang = app_module.get_stt_user_lang(123)
            assert lang == 'en'


def test_get_stt_user_lang_custom(mock_env):
    """Test getting user language with custom value"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import app as app_module
            app_module.users_collection = Mock()
            app_module.users_collection.find_one.return_value = {
                'user_id': '123',
                'stt_language': 'ru'
            }
            
            lang = app_module.get_stt_user_lang(123)
            assert lang == 'ru'


# ============================================================================
# Utility Functions Tests
# ============================================================================

def test_allowed_extensions(mock_env):
    """Test allowed file extensions"""
    with patch.dict(os.environ, mock_env):
        import app as app_module
        
        # Check common extensions are allowed
        assert 'mp3' in app_module.ALLOWED_EXTENSIONS
        assert 'wav' in app_module.ALLOWED_EXTENSIONS
        assert 'mp4' in app_module.ALLOWED_EXTENSIONS
        assert 'ogg' in app_module.ALLOWED_EXTENSIONS


def test_ffmpeg_binary_search(mock_env):
    """Test FFmpeg binary search"""
    with patch.dict(os.environ, mock_env):
        with patch('subprocess.run'):
            import app as app_module
            
            # FFmpeg binary should be searched
            assert hasattr(app_module, 'FFMPEG_BINARY')


# ============================================================================
# Threading Tests
# ============================================================================

def test_semaphore_initialization(mock_env):
    """Test transcript semaphore is initialized"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import app as app_module
            
            assert app_module.transcript_semaphore is not None
            assert app_module.transcript_semaphore._value == 2


def test_pending_queue_initialization(mock_env):
    """Test pending queue is initialized"""
    with patch.dict(os.environ, mock_env):
        with patch('pymongo.MongoClient'):
            import app as app_module
            
            assert app_module.PENDING_QUEUE is not None
            assert len(app_module.PENDING_QUEUE) == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
