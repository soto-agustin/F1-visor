// Trackside Pulse - Haptic and Audio/Visual Feedback Engine

class HapticsEngine {
  constructor() {
    this.audioCtx = null;
    this.audioEnabled = false;
    this.initAudio = this.initAudio.bind(this);
    
    // Unlock Web Audio API on first user interaction (standard mobile browser requirement)
    window.addEventListener('click', this.initAudio, { once: true });
    window.addEventListener('touchstart', this.initAudio, { once: true });
  }

  initAudio() {
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) {
        this.audioCtx = new AudioContext();
        this.audioEnabled = true;
      }
    } catch (e) {
      console.warn('Web Audio not supported:', e);
    }
  }

  playBeep(freq = 440, duration = 0.15) {
    if (!this.audioCtx || this.audioCtx.state === 'suspended') {
      try { this.audioCtx && this.audioCtx.resume(); } catch(e) {}
    }
    if (!this.audioCtx) return;

    try {
      const osc = this.audioCtx.createOscillator();
      const gain = this.audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, this.audioCtx.currentTime);
      gain.gain.setValueAtTime(0.2, this.audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, this.audioCtx.currentTime + duration);
      osc.connect(gain);
      gain.connect(this.audioCtx.destination);
      osc.start();
      osc.stop(this.audioCtx.currentTime + duration);
    } catch (e) {
      console.warn('Failed to play audio alert:', e);
    }
  }

  triggerVisualFlash() {
    document.body.classList.add('haptic-flash');
    setTimeout(() => {
      document.body.classList.remove('haptic-flash');
    }, 180);
  }

  // Type: 'pit' | 'flag_yellow' | 'flag_red'
  notify(type) {
    const hasVibrator = typeof navigator !== 'undefined' && 'vibrate' in navigator;

    switch (type) {
      case 'pit':
        if (hasVibrator) {
          navigator.vibrate([150, 50, 150]);
        } else {
          this.triggerVisualFlash();
          this.playBeep(520, 0.2);
        }
        break;

      case 'flag_yellow':
        if (hasVibrator) {
          navigator.vibrate([300, 100, 300]);
        } else {
          this.triggerVisualFlash();
          this.playBeep(440, 0.3);
        }
        break;

      case 'flag_red':
        if (hasVibrator) {
          navigator.vibrate([500, 200, 500]);
        } else {
          this.triggerVisualFlash();
          this.playBeep(330, 0.5);
        }
        break;

      default:
        if (hasVibrator) navigator.vibrate(80);
    }
  }
}

export const haptics = new HapticsEngine();
