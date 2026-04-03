// call_recordings.js — Individual user call recordings with sort/filter/date functionality

(function() {
    'use strict';

    var _allRecordings = [];
    var _filteredRecordings = [];
    var PAGE_SIZE = 20;
    var _currentPage = 0;
    var _loading = false;

    // ── Date range helpers ──────────────────────────────────────────────────
    function _todayStart() {
        var d = new Date();
        d.setHours(0, 0, 0, 0);
        return d;
    }

    function _dateRangeFor(filter) {
        var now = new Date();
        var today = _todayStart();
        switch (filter) {
            case 'today':
                return { start: today, end: now };
            case 'yesterday': {
                var yStart = new Date(today);
                yStart.setDate(yStart.getDate() - 1);
                return { start: yStart, end: today };
            }
            case 'week': {
                var wStart = new Date(today);
                wStart.setDate(wStart.getDate() - 7);
                return { start: wStart, end: now };
            }
            case 'last_week': {
                var lwEnd = new Date(today);
                lwEnd.setDate(lwEnd.getDate() - 7);
                var lwStart = new Date(lwEnd);
                lwStart.setDate(lwStart.getDate() - 7);
                return { start: lwStart, end: lwEnd };
            }
            case 'month': {
                var mStart = new Date(today.getFullYear(), today.getMonth(), 1);
                return { start: mStart, end: now };
            }
            default:
                return null; // no date restriction
        }
    }

    // ── Fetch all recordings once, then filter/sort client-side ────────────
    window.loadCallRecordings = function() {
        if (_loading) return;
        _loading = true;

        var container = document.getElementById('recordingsContainer');
        if (container) {
            container.innerHTML = '<div class="recordings-empty-state"><i class="fa-solid fa-spinner fa-spin"></i><span>Loading recordings...</span></div>';
        }

        fetch('/voice/call-history?limit=200')
            .then(function(r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function(data) {
                _allRecordings = data.calls || [];
                _loading = false;
                _applyFiltersAndRender();
            })
            .catch(function(e) {
                _loading = false;
                console.error('Call recordings error:', e);
                var c = document.getElementById('recordingsContainer');
                if (c) c.innerHTML = '<div class="recordings-empty-state"><i class="fa-solid fa-exclamation-triangle"></i><span>Error loading recordings</span></div>';
            });
    };

    // Called by filter/sort selects — re-filters cached data, no network call
    window.filterCallRecordings = function() {
        if (!_allRecordings.length && !_loading) {
            window.loadCallRecordings();
            return;
        }
        _applyFiltersAndRender();
    };

    function _applyFiltersAndRender() {
        var sortEl = document.getElementById('recordingsSortBy');
        var lengthEl = document.getElementById('recordingsLengthFilter');
        var dateEl = document.getElementById('recordingsDateFilter');

        var sortBy = sortEl ? sortEl.value : 'newest';
        var lengthFilter = lengthEl ? lengthEl.value : '';
        var dateFilter = dateEl ? dateEl.value : '';

        var range = _dateRangeFor(dateFilter);

        var filtered = _allRecordings.filter(function(r) {
            // Date filter
            if (range) {
                var d = new Date(r.created_at);
                if (d < range.start || d >= range.end) return false;
            }
            // Length filter
            var dur = r.duration || 0;
            if (lengthFilter === 'short' && dur >= 60) return false;
            if (lengthFilter === 'medium' && (dur < 60 || dur >= 300)) return false;
            if (lengthFilter === 'long' && dur < 300) return false;
            if (lengthFilter === 'has_recording' && !r.recording_url) return false;
            return true;
        });

        // Sort
        filtered.sort(function(a, b) {
            if (sortBy === 'duration_desc') return (b.duration || 0) - (a.duration || 0);
            if (sortBy === 'duration_asc') return (a.duration || 0) - (b.duration || 0);
            if (sortBy === 'contact_name') {
                return (a.contact_name || '').localeCompare(b.contact_name || '');
            }
            if (sortBy === 'oldest') return new Date(a.created_at) - new Date(b.created_at);
            // Default: newest first
            return new Date(b.created_at) - new Date(a.created_at);
        });

        _filteredRecordings = filtered;
        _currentPage = 0;
        _renderPage();
    }

    function _renderPage() {
        var container = document.getElementById('recordingsContainer');
        var pagination = document.getElementById('recordingsPagination');
        var pageInfo = document.getElementById('recordingsPageInfo');
        var prevBtn = document.getElementById('recordingsPrev');
        var nextBtn = document.getElementById('recordingsNext');
        var countEl = document.getElementById('recordingsCount');

        var total = _filteredRecordings.length;
        var totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

        if (countEl) {
            countEl.textContent = total + ' recording' + (total !== 1 ? 's' : '');
        }

        if (!total) {
            if (container) container.innerHTML = '<div class="recordings-empty-state"><i class="fa-solid fa-microphone-slash"></i><span>No recordings match your filters</span></div>';
            if (pagination) pagination.style.display = 'none';
            return;
        }

        var start = _currentPage * PAGE_SIZE;
        var pageItems = _filteredRecordings.slice(start, start + PAGE_SIZE);

        if (container) {
            container.innerHTML = pageItems.map(function(r) {
                var duration = r.duration || 0;
                var durationStr = _fmtTime(duration);
                var date = r.created_at ? new Date(r.created_at).toLocaleString() : '—';
                var hasRecording = !!r.recording_url;
                var hasTranscript = r.transcript && r.transcript.length > 0;
                // Encode URL safely for HTML attribute and inline JS
                var safeUrl = (r.recording_url || '').replace(/\\/g, '\\\\').replace(/'/g, '%27');

                var html = '<div class="recording-item" data-url="' + _escAttr(r.recording_url || '') + '">';
                html += '<div class="recording-header">';
                html += '<div class="recording-contact">' + _escHtml(r.contact_name || r.phone || 'Unknown') + '</div>';
                html += '<div class="recording-meta">' + date + ' &bull; ' + durationStr + '</div>';
                html += '</div>';

                if (hasRecording) {
                    html += '<div class="recording-controls">';
                    html += '<button onclick="playUserRecording(\'' + safeUrl + '\')" class="recording-btn recording-play-btn" title="Play"><i class="fa-solid fa-play"></i></button>';
                    html += '<button onclick="pauseUserRecording(\'' + safeUrl + '\')" class="recording-btn recording-pause-btn" title="Pause"><i class="fa-solid fa-pause"></i></button>';
                    html += '<button onclick="stopUserRecording(\'' + safeUrl + '\')" class="recording-btn recording-stop-btn" title="Stop"><i class="fa-solid fa-stop"></i></button>';
                    html += '<input type="range" class="recording-scrubber" min="0" max="100" value="0" oninput="seekUserRecording(\'' + safeUrl + '\', this.value)">';
                    html += '<span class="recording-current-time">0:00</span>';
                    html += '<span class="recording-duration">' + durationStr + '</span>';
                    var dlUrl = r.recording_url + (r.recording_url.indexOf('?') === -1 ? '.mp3' : '');
                    html += '<a href="' + _escAttr(dlUrl) + '" download class="recording-btn recording-download-btn" title="Download"><i class="fa-solid fa-download"></i></a>';
                    if (hasTranscript) {
                        html += '<button onclick=\'showUserTranscript(' + JSON.stringify(r.transcript).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/</g,'\\x3c') + ')\' class="recording-btn recording-transcript-btn" title="View Transcript"><i class="fa-solid fa-file-lines"></i></button>';
                    }
                    html += '</div>';
                }

                html += '</div>';
                return html;
            }).join('');
        }

        if (pageInfo) pageInfo.textContent = 'Page ' + (_currentPage + 1) + ' of ' + totalPages;
        if (prevBtn) prevBtn.disabled = _currentPage === 0;
        if (nextBtn) nextBtn.disabled = _currentPage >= totalPages - 1;
        if (pagination) pagination.style.display = totalPages > 1 ? 'flex' : 'none';
    }

    window.loadCallRecordingsPage = function(direction) {
        var totalPages = Math.max(1, Math.ceil(_filteredRecordings.length / PAGE_SIZE));
        _currentPage = Math.max(0, Math.min(_currentPage + direction, totalPages - 1));
        _renderPage();
    };

    // ── Audio Playback ──────────────────────────────────────────────────────
    var _currentUserAudio = null;
    var _currentUserAudioUrl = null;

    window.playUserRecording = function(url) {
        // Same recording — toggle play/pause
        if (_currentUserAudioUrl === url && _currentUserAudio) {
            if (_currentUserAudio.paused) {
                _currentUserAudio.play();
            } else {
                _currentUserAudio.pause();
            }
            return;
        }

        // Different recording — stop current first
        if (_currentUserAudio) {
            _currentUserAudio.pause();
            _currentUserAudio.currentTime = 0;
            _updateUserPlayBtn(_currentUserAudioUrl, false);
        }

        _currentUserAudio = new Audio(url);
        _currentUserAudioUrl = url;

        _currentUserAudio.addEventListener('loadedmetadata', function() {
            _updateUserDuration(url, _currentUserAudio.duration);
        });
        _currentUserAudio.addEventListener('timeupdate', function() {
            _updateUserScrubber(url, _currentUserAudio.currentTime, _currentUserAudio.duration);
            _updateUserCurrentTime(url, _currentUserAudio.currentTime);
        });
        _currentUserAudio.addEventListener('ended', function() {
            _updateUserPlayBtn(url, false);
            _currentUserAudio.currentTime = 0;
        });
        _currentUserAudio.addEventListener('pause', function() { _updateUserPlayBtn(url, false); });
        _currentUserAudio.addEventListener('play', function() { _updateUserPlayBtn(url, true); });

        _currentUserAudio.play();
    };

    window.pauseUserRecording = function(url) {
        if (_currentUserAudio && _currentUserAudioUrl === url) _currentUserAudio.pause();
    };

    window.stopUserRecording = function(url) {
        if (_currentUserAudio && _currentUserAudioUrl === url) {
            _currentUserAudio.pause();
            _currentUserAudio.currentTime = 0;
            _updateUserPlayBtn(url, false);
            _updateUserScrubber(url, 0, _currentUserAudio.duration || 0);
        }
    };

    window.seekUserRecording = function(url, pct) {
        if (_currentUserAudio && _currentUserAudioUrl === url && _currentUserAudio.duration) {
            _currentUserAudio.currentTime = (pct / 100) * _currentUserAudio.duration;
        }
    };

    function _itemEl(url, cls) {
        // data-url attribute uses raw URL (not %27 encoded) since we set it via _escAttr
        return document.querySelector('.recording-item[data-url="' + _escAttr(url) + '"] ' + cls);
    }

    function _updateUserPlayBtn(url, playing) {
        var play = _itemEl(url, '.recording-play-btn');
        var pause = _itemEl(url, '.recording-pause-btn');
        if (play) play.style.display = playing ? 'none' : 'inline-flex';
        if (pause) pause.style.display = playing ? 'inline-flex' : 'none';
    }

    function _updateUserDuration(url, secs) {
        var el = _itemEl(url, '.recording-duration');
        if (el) el.textContent = _fmtTime(secs);
    }

    function _updateUserCurrentTime(url, secs) {
        var el = _itemEl(url, '.recording-current-time');
        if (el) el.textContent = _fmtTime(secs);
    }

    function _updateUserScrubber(url, cur, dur) {
        var el = _itemEl(url, '.recording-scrubber');
        if (el) el.value = dur > 0 ? (cur / dur) * 100 : 0;
    }

    function _fmtTime(s) {
        if (!s || isNaN(s)) return '0:00';
        return Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0');
    }

    function _escHtml(s) {
        if (!s) return '';
        var d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    function _escAttr(s) {
        if (!s) return '';
        return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    // Transcript viewer — uses dialer.js showTranscript if available, else inline modal
    window.showUserTranscript = function(transcript) {
        if (typeof showTranscript === 'function') {
            showTranscript(transcript);
            return;
        }
        var modal = document.createElement('div');
        modal.className = 'transcript-modal';
        var text = typeof transcript === 'string' ? transcript : JSON.stringify(transcript, null, 2);
        modal.innerHTML = '<div class="transcript-modal-content">' +
            '<div class="transcript-modal-header"><h3>Call Transcript</h3>' +
            '<button onclick="this.closest(\'.transcript-modal\').remove()" class="transcript-close-btn">&times;</button></div>' +
            '<div class="transcript-modal-body"><pre>' + _escHtml(text) + '</pre></div></div>';
        document.body.appendChild(modal);
        modal.addEventListener('click', function(e) { if (e.target === modal) modal.remove(); });
    };

})();
