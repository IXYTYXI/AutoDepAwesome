<script setup>
import { ref, onMounted, onUnmounted } from "vue";

const canvas = ref(null);
let context, motion, finePointer;
let frame = 0,
  previous = 0,
  width = 0,
  height = 0;
let particles = [];
const mouse = { x: -1000, y: -1000 };

function createParticle() {
  const angle = Math.random() * Math.PI * 2;
  const speed = 5 + Math.random() * 9;
  return {
    x: Math.random() * width,
    y: Math.random() * height,
    vx: Math.cos(angle) * speed,
    vy: Math.sin(angle) * speed,
    radius: 1 + Math.random() * 1.5,
    phase: Math.random() * Math.PI * 2,
    sky: Math.random() > 0.65,
  };
}
function draw(time = 0, delta = 0) {
  context.clearRect(0, 0, width, height);
  const distance = width < 760 ? 105 : 155;
  for (const point of particles) {
    point.x += point.vx * delta;
    point.y += point.vy * delta;
    const dx = point.x - mouse.x,
      dy = point.y - mouse.y;
    const proximity = Math.hypot(dx, dy);
    if (delta && proximity > 0 && proximity < 135) {
      const force = (1 - proximity / 135) * 75 * delta;
      point.x += (dx / proximity) * force;
      point.y += (dy / proximity) * force;
    }
    if (point.x < -12) point.x = width + 12;
    if (point.x > width + 12) point.x = -12;
    if (point.y < -12) point.y = height + 12;
    if (point.y > height + 12) point.y = -12;
  }
  for (let i = 0; i < particles.length; i++) {
    const p = particles[i];
    let connections = 0;
    for (let j = i + 1; j < particles.length && connections < 3; j++) {
      const q = particles[j],
        gap = Math.hypot(p.x - q.x, p.y - q.y);
      if (gap >= distance) continue;
      context.strokeStyle = `rgba(51,114,197,${0.24 * (1 - gap / distance)})`;
      context.lineWidth = 0.7;
      context.beginPath();
      context.moveTo(p.x, p.y);
      context.lineTo(q.x, q.y);
      context.stroke();
      connections++;
    }
    const glow = 0.46 + Math.sin(time * 0.0006 + p.phase) * 0.12;
    context.beginPath();
    context.arc(p.x, p.y, p.radius + 3, 0, Math.PI * 2);
    context.fillStyle = `rgba(88,163,235,${glow * 0.12})`;
    context.fill();
    context.beginPath();
    context.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
    context.fillStyle = p.sky
      ? `rgba(52,167,221,${glow})`
      : `rgba(47,104,191,${glow})`;
    context.fill();
    if (p.radius > 2) {
      context.beginPath();
      context.arc(p.x - 0.35, p.y - 0.35, 0.65, 0, Math.PI * 2);
      context.fillStyle = "rgba(255,255,255,.8)";
      context.fill();
    }
  }
}
function tick(time) {
  if (!previous || time - previous >= 1000 / 30) {
    const delta = previous ? Math.min((time - previous) / 1000, 0.05) : 0;
    previous = time;
    draw(time, delta);
  }
  frame = requestAnimationFrame(tick);
}
function restart() {
  cancelAnimationFrame(frame);
  frame = 0;
  previous = 0;
  mouse.x = mouse.y = -1000;
  if (!context || document.hidden) return;
  draw();
  if (!motion.matches) frame = requestAnimationFrame(tick);
}
function resize() {
  width = innerWidth;
  height = innerHeight;
  const ratio = Math.min(devicePixelRatio || 1, 1.5);
  canvas.value.width = Math.round(width * ratio);
  canvas.value.height = Math.round(height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  const count = Math.min(
    width < 760 ? 42 : 120,
    Math.max(30, Math.round((width * height) / 12000)),
  );
  particles = Array.from({ length: count }, createParticle);
  restart();
}
function move(event) {
  if (motion.matches || !finePointer.matches || event.pointerType === "touch")
    return;
  mouse.x = event.clientX;
  mouse.y = event.clientY;
}
function leave() {
  mouse.x = mouse.y = -1000;
}
onMounted(() => {
  context = canvas.value.getContext("2d");
  if (!context) return;
  motion = matchMedia("(prefers-reduced-motion: reduce)");
  finePointer = matchMedia("(hover: hover) and (pointer: fine)");
  resize();
  motion.addEventListener("change", restart);
  finePointer.addEventListener("change", leave);
  window.addEventListener("resize", resize, { passive: true });
  window.addEventListener("pointermove", move, { passive: true });
  document.documentElement.addEventListener("pointerleave", leave);
  document.addEventListener("visibilitychange", restart);
});
onUnmounted(() => {
  cancelAnimationFrame(frame);
  motion?.removeEventListener("change", restart);
  finePointer?.removeEventListener("change", leave);
  window.removeEventListener("resize", resize);
  window.removeEventListener("pointermove", move);
  document.documentElement.removeEventListener("pointerleave", leave);
  document.removeEventListener("visibilitychange", restart);
});
</script>

<template>
  <Teleport to="body">
    <canvas
      ref="canvas"
      class="particle-background"
      aria-hidden="true"
    ></canvas>
  </Teleport>
</template>
