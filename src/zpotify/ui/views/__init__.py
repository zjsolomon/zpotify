"""Views: one per tab, composed by ui.app.App."""

from __future__ import annotations

from zpotify.ui.views.base import View
from zpotify.ui.views.now_playing import NowPlayingView
from zpotify.ui.views.search import SearchView
from zpotify.ui.views.playlists import PlaylistsView
from zpotify.ui.views.library import LibraryView
from zpotify.ui.views.queue import QueueView
from zpotify.ui.views.devices import DevicesView
from zpotify.ui.views.settings import SettingsView

__all__ = [
    "View", "NowPlayingView", "SearchView", "PlaylistsView",
    "LibraryView", "QueueView", "DevicesView", "SettingsView",
]
