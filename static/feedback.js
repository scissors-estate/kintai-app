// 打刻・申請時のフィードバック（アクション別の演出）
(function () {
  function vibrate(pattern) {
    if (navigator.vibrate) {
      try { navigator.vibrate(pattern || [60, 40, 60]); } catch (e) {}
    }
  }

  function beep(freq, duration) {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      const ctx = new AC();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = "sine";
      osc.frequency.value = freq || 880;
      gain.gain.setValueAtTime(0.18, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + (duration || 0.18));
      osc.start();
      osc.stop(ctx.currentTime + (duration || 0.18));
    } catch (e) {}
  }

  // 3音階でちょっと華やかに（退勤用）
  function chord() {
    [523.25, 659.25, 783.99].forEach((f, i) => {
      setTimeout(() => beep(f, 0.25), i * 80);
    });
  }

  function confettiBurst() {
    if (!window.confetti) return;
    const defaults = { startVelocity: 35, spread: 360, ticks: 80, zIndex: 10000 };
    window.confetti({ ...defaults, particleCount: 60, origin: { x: 0.2, y: 0.3 } });
    window.confetti({ ...defaults, particleCount: 60, origin: { x: 0.8, y: 0.3 } });
    setTimeout(() => {
      window.confetti({ ...defaults, particleCount: 40, origin: { x: 0.5, y: 0.2 } });
    }, 200);
  }

  /**
   * オーバーレイ表示
   * style: { theme, animation, font, mark, msg, pulse }
   */
  function showOverlay(style) {
    const div = document.createElement("div");
    div.className = "feedback-overlay"
      + (style.theme ? " theme-" + style.theme : "")
      + (style.animation ? " fb-" + style.animation : " fb-pop")
      + (style.pulse ? " pulse" : "");

    const mark = document.createElement("div");
    mark.className = "mark " + (style.font || "font-mincho");
    mark.textContent = style.mark || "✓";

    const msg = document.createElement("div");
    msg.className = "msg " + (style.font || "font-mincho");
    msg.textContent = style.msg || "完了";

    div.appendChild(mark);
    div.appendChild(msg);
    document.body.appendChild(div);
    requestAnimationFrame(() => div.classList.add("show"));
    return div;
  }

  // 打刻種別 → 演出スタイル
  function styleForPunch(type) {
    if (type === "in") {
      const h = new Date().getHours();
      if (h >= 5 && h < 10) {
        return { theme: "morning", animation: "drop", font: "font-mincho",
                 mark: "☀", msg: "おはようございます", pulse: true,
                 vibe: [100, 50, 100], sound: () => beep(880, 0.2), hold: 900 };
      } else if (h >= 10 && h < 16) {
        return { theme: "noon", animation: "drop", font: "font-mincho",
                 mark: "✿", msg: "こんにちは", pulse: true,
                 vibe: [80, 40, 80], sound: () => beep(880, 0.2), hold: 900 };
      } else if (h >= 16 && h < 22) {
        return { theme: "evening", animation: "drop", font: "font-mincho",
                 mark: "✦", msg: "こんばんは", pulse: true,
                 vibe: [80, 40, 80], sound: () => beep(660, 0.25), hold: 900 };
      } else {
        return { theme: "night", animation: "drop", font: "font-mincho",
                 mark: "☾", msg: "遅くまでお疲れさまです",
                 vibe: [80, 40, 80], sound: () => beep(520, 0.3), hold: 900 };
      }
    }
    if (type === "out") {
      return { theme: "leave", animation: "celebrate", font: "font-gothic",
               mark: "🎉", msg: "お疲れさまでした",
               vibe: [120, 60, 120, 60, 120],
               sound: chord, confetti: true, hold: 1400 };
    }
    if (type === "break_in") {
      return { theme: "break", animation: "poyon", font: "font-klee",
               mark: "☕", msg: "ゆっくり休んでね",
               vibe: [60], sound: () => beep(700, 0.2), hold: 800 };
    }
    if (type === "break_out") {
      return { theme: "resume", animation: "slide", font: "font-klee",
               mark: "✨", msg: "おかえりなさい",
               vibe: [60], sound: () => beep(900, 0.2), hold: 800 };
    }
    return { theme: "submit", animation: "pop", font: "font-mincho-italic",
             mark: "✓", msg: "完了", vibe: [60], sound: () => beep(880, 0.18), hold: 500 };
  }

  /**
   * フォーム送信時にアクション別フィードバックを表示
   */
  window.attachFeedback = function (selector, optsOrFn) {
    document.querySelectorAll(selector).forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (form.dataset._sent) return;
        form.dataset._sent = "1";
        e.preventDefault();

        const style = (typeof optsOrFn === "function")
          ? optsOrFn(e) : (optsOrFn || {});

        vibrate(style.vibe);
        if (typeof style.sound === "function") style.sound();
        if (style.confetti) confettiBurst();
        showOverlay(style);

        setTimeout(function () { form.submit(); }, style.hold || 500);
      });
    });
  };

  window.styleForPunch = styleForPunch;
})();
