// Trackside Pulse - Local Reactive State Store

export const state = {
  seasonData: null,
  favoriteDriver: localStorage.getItem('trackside_fav_driver') || 'VER',
  currentCircuit: 'interlagos',
  gridSegmentMode: 'top11', // 'top11' | 'bottom11' | 'all'
  raceState: null,
  lastEtag: null,
  lastTs: null,
  lastSuccessfulFetch: Date.now(),
  isStale: false,
  isOffline: !navigator.onLine,
  listeners: new Set(),

  setGridSegmentMode(mode) {
    if (this.gridSegmentMode !== mode) {
      this.gridSegmentMode = mode;
      this.notify();
    }
  },

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  },

  notify() {
    for (const listener of this.listeners) {
      try {
        listener(this);
      } catch (err) {
        console.error('Error in state listener:', err);
      }
    }
  },

  setFavoriteDriver(code) {
    if (this.favoriteDriver !== code) {
      this.favoriteDriver = code;
      localStorage.setItem('trackside_fav_driver', code);
      this.notify();
    }
  },

  setSeasonData(data) {
    this.seasonData = data;
    this.notify();
  },

  updateRaceState(newState, etag) {
    if (etag) this.lastEtag = etag;
    this.raceState = newState;
    if (newState?.circuit) this.currentCircuit = newState.circuit;
    this.lastSuccessfulFetch = Date.now();
    this.isStale = false;
    this.notify();
  },

  markStale(stale = true) {
    if (this.isStale !== stale) {
      this.isStale = stale;
      this.notify();
    }
  },

  setOffline(offline) {
    if (this.isOffline !== offline) {
      this.isOffline = offline;
      this.notify();
    }
  }
};
