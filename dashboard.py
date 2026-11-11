#!/usr/bin/env python3
"""
dashboard.py
============
Clean, Lightweight & Minimalist Real-Time SDN QoS Web Dashboard.

Provides:
  - Clean real-time telemetry (Throughput, Loss, Latency, Jain's Fairness Index).
  - Live Chart.js visualization of OpenFlow 1.3 HTB queue enforcement.
  - Automatic detection and visualization of Phase 1 -> Phase 2 dynamic policy swap.
  - Interactive Experiment Studio with custom traffic presets and sliders.
  - Monospace event terminal displaying SDN controller events.
  - Zero Python dependencies (built entirely on standard library http.server).

Usage:
  python3 dashboard.py [--port 8080]
  Open http://localhost:8080 in your browser.
"""

import os
import json
import time
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler

STATS_FILE = "/tmp/qos_stats.json"
LOGS_FILE = "/tmp/qos_logs.txt"
CMD_FILE = "/tmp/qos_cmd.txt"

PENDING_COMMAND = None

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SDN QoS Traffic Management</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      --bg: #090d16;
      --surface: #111726;
      --surface-sub: #172033;
      --border: #1e293b;
      --border-focus: #334155;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --accent-h1: #ef4444;
      --accent-h2: #f59e0b;
      --accent-h4: #3b82f6;
      --emerald: #10b981;
      --cyan: #06b6d4;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 20px; min-height: 100vh; }
    .container { max-width: 1400px; margin: 0 auto; display: flex; flex-direction: column; gap: 18px; }

    /* Top Nav / Header */
    header {
      display: flex; justify-content: space-between; align-items: center;
      background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
      padding: 16px 22px;
    }
    .brand h1 { font-size: 20px; font-weight: 700; color: #38bdf8; letter-spacing: -0.3px; }
    .brand p { font-size: 13px; color: var(--text-muted); margin-top: 2px; }
    .header-actions { display: flex; align-items: center; gap: 12px; }

    .status-pill {
      display: inline-flex; align-items: center; gap: 7px;
      padding: 6px 12px; border-radius: 9999px; font-size: 12px; font-weight: 600;
      background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .pulse-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; animation: pulse 1.6s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }

    .btn {
      cursor: pointer; border: none; border-radius: 8px; font-size: 12px; font-weight: 600;
      padding: 8px 16px; transition: all 0.15s ease; display: inline-flex; align-items: center; gap: 6px;
    }
    .btn-start { background: var(--emerald); color: #042f1a; }
    .btn-start:hover { background: #059669; color: #fff; }
    .btn-stop { background: #dc2626; color: #fff; }
    .btn-stop:hover { background: #b91c1c; }
    .btn-secondary { background: var(--surface-sub); color: var(--text); border: 1px solid var(--border); }
    .btn-secondary:hover { background: #1e293b; border-color: var(--border-focus); }

    /* KPI Cards */
    .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
    .kpi-card {
      background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px;
      display: flex; flex-direction: column; justify-content: space-between;
    }
    .kpi-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
    .kpi-val { font-size: 24px; font-weight: 700; margin: 8px 0 4px; color: var(--text); }
    .kpi-sub { font-size: 12px; color: var(--text-dim); }

    .progress-track { height: 6px; background: var(--surface-sub); border-radius: 3px; overflow: hidden; margin-top: 8px; }
    .progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #38bdf8, #ef4444); transition: width 0.4s ease; }

    /* Policy Banner */
    .policy-banner {
      background: var(--surface); border: 1px solid var(--border); border-left: 4px solid #38bdf8;
      border-radius: 10px; padding: 12px 18px; display: flex; align-items: center; justify-content: space-between;
      transition: all 0.3s ease;
    }
    .policy-info { display: flex; align-items: center; gap: 10px; font-size: 13px; }
    .policy-title { font-weight: 700; color: #38bdf8; }
    .policy-desc { font-weight: 500; color: var(--text); }
    .policy-badge {
      font-size: 11px; padding: 4px 10px; border-radius: 6px; font-weight: 600;
      background: var(--surface-sub); color: var(--text-muted); border: 1px solid var(--border);
    }

    /* Main 2-Column Grid */
    .main-grid { display: grid; grid-template-columns: 1.5fr 1fr; gap: 18px; }
    @media (max-width: 1024px) { .main-grid { grid-template-columns: 1fr; } }

    .card {
      background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px;
      display: flex; flex-direction: column; gap: 14px;
    }
    .card-header { display: flex; justify-content: space-between; align-items: center; }
    .card-title { font-size: 14px; font-weight: 600; color: var(--text); display: flex; align-items: center; gap: 8px; }

    /* Telemetry Table */
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { text-align: left; padding: 8px 10px; color: var(--text-muted); font-size: 11px; text-transform: uppercase; border-bottom: 1px solid var(--border); }
    td { padding: 11px 10px; border-bottom: 1px solid rgba(30, 41, 59, 0.5); }
    tr:last-child td { border-bottom: none; }
    .tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }
    .tag-h1 { background: rgba(239, 68, 68, 0.15); color: #fca5a5; }
    .tag-h2 { background: rgba(245, 158, 11, 0.15); color: #fde68a; }
    .tag-h4 { background: rgba(59, 130, 246, 0.15); color: #bfdbfe; }

    /* Minimal Topology Map */
    .topo-row {
      display: flex; align-items: center; justify-content: space-between; background: #0b101c;
      border: 1px solid var(--border); border-radius: 10px; padding: 12px 18px;
    }
    .topo-hosts { display: flex; flex-direction: column; gap: 6px; }
    .topo-node { font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 6px; text-align: center; }
    .topo-link { flex: 1; margin: 0 14px; text-align: center; }
    .topo-link-pipe { height: 8px; background: var(--surface-sub); border-radius: 4px; overflow: hidden; position: relative; margin-top: 4px; }
    .topo-link-fill { height: 100%; width: 0%; background: linear-gradient(90deg, #38bdf8, #ef4444); transition: width 0.4s ease; }

    /* Studio Controls */
    .preset-group { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
    .btn-preset { font-size: 11px; padding: 5px 10px; background: var(--surface-sub); color: var(--text-muted); border: 1px solid var(--border); border-radius: 6px; }
    .btn-preset:hover { background: #1e293b; color: var(--text); border-color: var(--border-focus); }
    .slider-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; font-size: 12px; }
    .slider-row label { flex: 1; color: var(--text-muted); }
    .slider-row input[type="range"] { flex: 1.5; accent-color: #38bdf8; height: 5px; }
    .slider-val { width: 50px; text-align: right; font-weight: 600; color: #38bdf8; }

    /* Logs Terminal */
    .log-box {
      background: #060a12; border: 1px solid var(--border); border-radius: 8px;
      padding: 12px; font-family: "JetBrains Mono", Menlo, Consolas, monospace;
      font-size: 11px; height: 210px; overflow-y: auto; line-height: 1.6;
    }
    .log-line { color: #94a3b8; word-break: break-all; }
    .log-ts { color: #475569; margin-right: 6px; }
    .log-dyn { color: #f59e0b; font-weight: 600; }
    .log-ovs { color: #38bdf8; }
    .log-soc { color: #34d399; }
  </style>
</head>
<body>

<div class="container">

  <!-- Header -->
  <header>
    <div class="brand">
      <h1>SDN QoS Traffic Manager</h1>
      <p>OpenFlow 1.3 & Mininet Linux HTB Queue Telemetry</p>
    </div>
    <div class="header-actions">
      <span class="status-pill"><span class="pulse-dot"></span> Live Network</span>
      <button id="btn-start" class="btn btn-start" onclick="startTraffic()">▶ Start Traffic (40s)</button>
      <button id="btn-stop" class="btn btn-stop" onclick="stopTraffic()">⏹ Stop</button>
    </div>
  </header>

  <!-- KPI Row -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-title">Bottleneck Utilization</div>
      <div class="kpi-val"><span id="val-total-rate">0.00</span> <span style="font-size: 14px; color: var(--text-dim);">/ 10.00 Mbps</span></div>
      <div class="progress-track"><div id="bar-total-load" class="progress-bar"></div></div>
      <div class="kpi-sub" style="margin-top: 6px;"><span id="val-load-pct">0%</span> of 10 Mbps capacity</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Jain's Fairness Index</div>
      <div class="kpi-val" id="val-jain">1.000</div>
      <div class="kpi-sub" id="sub-jain">Idle / No Contention</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Active UDP Flows</div>
      <div class="kpi-val" id="val-active-flows">0 <span style="font-size: 14px; color: var(--text-dim);">/ 3</span></div>
      <div class="kpi-sub">h1 (5001), h2 (5002), h4 (5003)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">SDN Policy Status</div>
      <div class="kpi-val" id="val-phase-title" style="font-size: 18px; color: #38bdf8;">Phase 1</div>
      <div class="kpi-sub" id="val-phase-sub">h1 High (6M) | h4 Low (1M)</div>
    </div>
  </div>

  <!-- Active Policy Banner -->
  <div class="policy-banner" id="policy-banner">
    <div class="policy-info">
      <span class="policy-title">POLICY ENFORCEMENT:</span>
      <span class="policy-desc" id="policy-text">Phase 1 — Initial Priority (h1: 6 Mbps, h2: 3 Mbps, h4: 1 Mbps)</span>
    </div>
    <span class="policy-badge" id="policy-timer-badge">Swap fires at t=20s</span>
  </div>

  <!-- Topology Row -->
  <div class="topo-row">
    <div class="topo-hosts">
      <div class="topo-node tag-h1" id="topo-h1">h1: High (6M)</div>
      <div class="topo-node tag-h2" id="topo-h2">h2: Med (3M)</div>
      <div class="topo-node tag-h4" id="topo-h4">h4: Bulk (1M)</div>
    </div>
    <div style="font-size: 12px; font-weight: 700; color: var(--text-dim); margin: 0 8px;">➔ [ Switch s1 ]</div>
    <div class="topo-link">
      <div style="font-size: 11px; color: var(--text-muted); font-weight: 600;">Bottleneck Egress Link (s1-eth4 ➔ s2)</div>
      <div class="topo-link-pipe"><div id="topo-pipe-fill" class="topo-link-fill"></div></div>
      <div style="font-size: 10px; color: var(--text-dim); margin-top: 2px;" id="topo-link-text">0.00 Mbps (0% Load)</div>
    </div>
    <div style="font-size: 12px; font-weight: 700; color: var(--text-dim); margin: 0 8px;">➔ [ Switch s2 ] ➔</div>
    <div class="topo-node" style="background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3);">
      h3: UDP Sink
    </div>
  </div>

  <!-- Main 2-Column Grid -->
  <div class="main-grid">
    
    <!-- Left: Throughput Chart & Telemetry Table -->
    <div style="display: flex; flex-direction: column; gap: 18px;">
      
      <!-- Chart -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Real-Time Throughput (Mbps)</span>
          <span style="font-size: 11px; color: var(--text-dim);">OpenFlow 1.3 HTB Egress Monitoring</span>
        </div>
        <div style="position: relative; height: 260px; width: 100%;">
          <canvas id="throughputChart"></canvas>
        </div>
      </div>

      <!-- Table -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Live Flow Telemetry</span>
          <span style="font-size: 11px; color: var(--text-dim);">Receiver metrics on h3 (10.0.0.3)</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Traffic Class / Host</th>
              <th>Port</th>
              <th>Throughput</th>
              <th>Packet Loss</th>
              <th>Latency (RTT)</th>
              <th>OVS Queue</th>
            </tr>
          </thead>
          <tbody id="telemetry-tbody">
            <tr>
              <td><span class="tag tag-h1">Host h1 (VoIP)</span></td>
              <td>5001</td>
              <td id="t-h1-rate">0.00 Mbps</td>
              <td id="t-h1-loss">0.0%</td>
              <td id="t-h1-lat">0.0 ms</td>
              <td id="t-h1-q">Queue 0 (6M)</td>
            </tr>
            <tr>
              <td><span class="tag tag-h2">Host h2 (Video)</span></td>
              <td>5002</td>
              <td id="t-h2-rate">0.00 Mbps</td>
              <td id="t-h2-loss">0.0%</td>
              <td id="t-h2-lat">0.0 ms</td>
              <td>Queue 1 (3M)</td>
            </tr>
            <tr>
              <td><span class="tag tag-h4">Host h4 (Bulk)</span></td>
              <td>5003</td>
              <td id="t-h4-rate">0.00 Mbps</td>
              <td id="t-h4-loss">0.0%</td>
              <td id="t-h4-lat">0.0 ms</td>
              <td id="t-h4-q">Queue 2 (1M)</td>
            </tr>
          </tbody>
        </table>
      </div>

    </div>

    <!-- Right: Studio & Controller Logs -->
    <div style="display: flex; flex-direction: column; gap: 18px;">
      
      <!-- Interactive Studio -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Interactive Experiment Studio</span>
          <span style="font-size: 11px; color: var(--text-dim);">Live Parameters</span>
        </div>
        
        <div class="preset-group">
          <button class="btn-preset" onclick="setPreset('benchmark')">Standard (10M each)</button>
          <button class="btn-preset" onclick="setPreset('overload')">Severe Overload (15M each)</button>
          <button class="btn-preset" onclick="setPreset('two_flow')">2-Flow Contention (h1 & h2)</button>
          <button class="btn-preset" onclick="setPreset('single')">Single Baseline (h1 only)</button>
        </div>

        <div class="slider-row">
          <label><input type="checkbox" id="chk-h1" checked> h1 Target Rate:</label>
          <input type="range" id="rate-h1" min="1" max="20" value="10" oninput="updateSliderLabels()">
          <span class="slider-val" id="val-rate-h1">10M</span>
        </div>
        <div class="slider-row">
          <label><input type="checkbox" id="chk-h2" checked> h2 Target Rate:</label>
          <input type="range" id="rate-h2" min="1" max="20" value="10" oninput="updateSliderLabels()">
          <span class="slider-val" id="val-rate-h2">10M</span>
        </div>
        <div class="slider-row">
          <label><input type="checkbox" id="chk-h4" checked> h4 Target Rate:</label>
          <input type="range" id="rate-h4" min="1" max="20" value="10" oninput="updateSliderLabels()">
          <span class="slider-val" id="val-rate-h4">10M</span>
        </div>
        <div class="slider-row" style="margin-top: 10px; border-top: 1px solid var(--border); padding-top: 10px;">
          <label>Experiment Duration:</label>
          <input type="range" id="exp-dur" min="10" max="90" step="5" value="40" oninput="updateSliderLabels()">
          <span class="slider-val" id="val-exp-dur">40s</span>
        </div>

        <button class="btn btn-secondary" style="margin-top: 8px; justify-content: center; background: #1e293b; color: #38bdf8; border-color: #38bdf8;" onclick="launchCustomExperiment()">
          🚀 Launch Custom Experiment
        </button>
      </div>

      <!-- Logs -->
      <div class="card" style="flex: 1;">
        <div class="card-header">
          <span class="card-title">SDN Controller & Event Logs</span>
          <button class="btn btn-secondary" style="padding: 3px 8px; font-size: 11px;" onclick="clearLogs()">Clear</button>
        </div>
        <div class="log-box" id="log-box">
          <div class="log-line" style="color: #64748b;">Ready. Start traffic to stream controller events.</div>
        </div>
      </div>

    </div>

  </div>

</div>

<script>
  // Chart initialization
  const ctx = document.getElementById('throughputChart').getContext('2d');
  const MAX_POINTS = 30;
  const labels = Array.from({length: MAX_POINTS}, (_, i) => `${i - MAX_POINTS + 1}s`);

  const chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'h1 High (VoIP)',
          data: new Array(MAX_POINTS).fill(0),
          borderColor: '#ef4444',
          backgroundColor: 'rgba(239, 68, 68, 0.1)',
          borderWidth: 2,
          tension: 0.3,
          pointRadius: 2,
          pointHoverRadius: 4,
          fill: true
        },
        {
          label: 'h2 Med (Video)',
          data: new Array(MAX_POINTS).fill(0),
          borderColor: '#f59e0b',
          backgroundColor: 'rgba(245, 158, 11, 0.08)',
          borderWidth: 2,
          tension: 0.3,
          pointRadius: 2,
          pointHoverRadius: 4,
          fill: true
        },
        {
          label: 'h4 Low (Bulk)',
          data: new Array(MAX_POINTS).fill(0),
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59, 130, 246, 0.08)',
          borderWidth: 2,
          tension: 0.3,
          pointRadius: 2,
          pointHoverRadius: 4,
          fill: true
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      plugins: {
        legend: {
          position: 'top',
          labels: { color: '#94a3b8', font: { size: 11, weight: '600' }, boxWidth: 10, usePointStyle: true }
        },
        tooltip: {
          backgroundColor: '#0f172a',
          borderColor: '#334155',
          borderWidth: 1,
          titleFont: { size: 11 },
          bodyFont: { size: 11 }
        }
      },
      scales: {
        x: {
          grid: { color: '#172033' },
          ticks: { color: '#64748b', font: { size: 10 }, maxTicksLimit: 10 }
        },
        y: {
          min: 0,
          max: 9,
          grid: { color: '#172033' },
          ticks: {
            color: '#64748b',
            font: { size: 10 },
            stepSize: 1,
            callback: v => v + ' M'
          }
        }
      }
    }
  });

  let currentPhase = 1;
  let swapCountdownTimer = null;

  function updateSliderLabels() {
    document.getElementById('val-rate-h1').innerText = document.getElementById('rate-h1').value + 'M';
    document.getElementById('val-rate-h2').innerText = document.getElementById('rate-h2').value + 'M';
    document.getElementById('val-rate-h4').innerText = document.getElementById('rate-h4').value + 'M';
    document.getElementById('val-exp-dur').innerText = document.getElementById('exp-dur').value + 's';
  }

  function setPreset(name) {
    const h1 = document.getElementById('rate-h1');
    const h2 = document.getElementById('rate-h2');
    const h4 = document.getElementById('rate-h4');
    const chk1 = document.getElementById('chk-h1');
    const chk2 = document.getElementById('chk-h2');
    const chk4 = document.getElementById('chk-h4');
    const dur = document.getElementById('exp-dur');

    chk1.checked = chk2.checked = chk4.checked = true;
    dur.value = 40;

    if (name === 'benchmark') {
      h1.value = 10; h2.value = 10; h4.value = 10;
    } else if (name === 'overload') {
      h1.value = 15; h2.value = 15; h4.value = 15;
    } else if (name === 'two_flow') {
      h1.value = 10; h2.value = 10; h4.value = 10;
      chk4.checked = false;
    } else if (name === 'single') {
      h1.value = 10; h2.value = 10; h4.value = 10;
      chk2.checked = false; chk4.checked = false;
    }
    updateSliderLabels();
  }

  function setPhaseUI(phase) {
    currentPhase = phase;
    const banner = document.getElementById('policy-banner');
    const pText = document.getElementById('policy-text');
    const pBadge = document.getElementById('policy-timer-badge');
    const title = document.getElementById('val-phase-title');
    const sub = document.getElementById('val-phase-sub');
    const topoH1 = document.getElementById('topo-h1');
    const topoH4 = document.getElementById('topo-h4');
    const tH1Q = document.getElementById('t-h1-q');
    const tH4Q = document.getElementById('t-h4-q');

    if (phase === 1) {
      banner.style.borderLeftColor = '#38bdf8';
      banner.style.background = 'var(--surface)';
      pText.innerText = "Phase 1 — Initial Priority (h1: 6 Mbps, h2: 3 Mbps, h4: 1 Mbps)";
      pBadge.innerText = "Swap fires at t=20s";
      title.innerText = "Phase 1";
      title.style.color = "#38bdf8";
      sub.innerText = "h1 High (6M) | h4 Low (1M)";
      topoH1.innerText = "h1: High (6M)";
      topoH4.innerText = "h4: Bulk (1M)";
      tH1Q.innerText = "Queue 0 (6M)";
      tH4Q.innerText = "Queue 2 (1M)";
    } else {
      banner.style.borderLeftColor = '#f59e0b';
      banner.style.background = 'rgba(245, 158, 11, 0.08)';
      pText.innerText = "Phase 2 — Dynamic Policy Swapped! (h1 Demoted: 1 Mbps, h4 Promoted: 6 Mbps)";
      pBadge.innerText = "Active Phase 2";
      title.innerText = "Phase 2 (Swapped)";
      title.style.color = "#f59e0b";
      sub.innerText = "h4 Promoted (6M) | h1 Demoted (1M)";
      topoH1.innerText = "h1: Demoted (1M)";
      topoH4.innerText = "h4: Promoted (6M)";
      tH1Q.innerText = "Queue 0 (1M) [Demoted]";
      tH4Q.innerText = "Queue 2 (6M) [Promoted]";
    }
  }

  function startSwapCountdown(swapSec = 20) {
    if (swapCountdownTimer) clearInterval(swapCountdownTimer);
    let remaining = swapSec;
    const badge = document.getElementById('policy-timer-badge');
    setPhaseUI(1);
    badge.innerText = `Dynamic swap in ${remaining}s...`;

    swapCountdownTimer = setInterval(() => {
      remaining--;
      if (remaining > 0) {
        badge.innerText = `Dynamic swap in ${remaining}s...`;
      } else {
        clearInterval(swapCountdownTimer);
        setPhaseUI(2);
      }
    }, 1000);
  }

  function resetStatsVisuals() {
    chart.data.datasets.forEach(ds => ds.data.fill(0));
    chart.update();

    document.getElementById('val-total-rate').innerText = "0.00";
    document.getElementById('val-load-pct').innerText = "0%";
    document.getElementById('bar-total-load').style.width = "0%";
    document.getElementById('topo-pipe-fill').style.width = "0%";
    document.getElementById('topo-link-text').innerText = "0.00 Mbps (0% Load)";
    document.getElementById('val-jain').innerText = "1.000";
    document.getElementById('sub-jain').innerText = "Idle / No Contention";
    document.getElementById('val-active-flows').innerHTML = '0 <span style="font-size: 14px; color: var(--text-dim);">/ 3</span>';

    ['h1', 'h2', 'h4'].forEach(h => {
      document.getElementById(`t-${h}-rate`).innerText = "0.00 Mbps";
      document.getElementById(`t-${h}-loss`).innerText = "0.0%";
      document.getElementById(`t-${h}-lat`).innerText = "0.0 ms";
    });
  }

  function startTraffic() {
    resetStatsVisuals();
    startSwapCountdown(20);
    fetch('/api/action/start', { method: 'POST', body: '40' });
  }

  function stopTraffic() {
    if (swapCountdownTimer) clearInterval(swapCountdownTimer);
    fetch('/api/action/stop', { method: 'POST' });
    resetStatsVisuals();
    setPhaseUI(1);
    document.getElementById('policy-timer-badge').innerText = "Stopped";
  }

  function launchCustomExperiment() {
    const dur = parseInt(document.getElementById('exp-dur').value) || 40;
    const cfg = {
      duration: dur,
      h1: { enabled: document.getElementById('chk-h1').checked, rate: parseInt(document.getElementById('rate-h1').value) },
      h2: { enabled: document.getElementById('chk-h2').checked, rate: parseInt(document.getElementById('rate-h2').value) },
      h4: { enabled: document.getElementById('chk-h4').checked, rate: parseInt(document.getElementById('rate-h4').value) }
    };
    resetStatsVisuals();
    startSwapCountdown(20);
    fetch('/api/action/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg)
    });
  }

  function clearLogs() {
    fetch('/api/logs/clear', { method: 'POST' });
    document.getElementById('log-box').innerHTML = '<div class="log-line" style="color: #64748b;">Logs cleared.</div>';
  }

  // Polling loop: Telemetry stats
  async function fetchStats() {
    try {
      const resp = await fetch('/api/stats');
      if (!resp.ok) return;
      const data = await resp.json();

      let r1 = 0, r2 = 0, r4 = 0;
      let activeCount = 0;

      (data.flows || []).forEach(f => {
        const mbps = parseFloat(f.mbps) || 0;
        const loss = parseFloat(f.loss_pct) || 0;
        const lat = parseFloat(f.latency_ms) || 0;

        if (mbps > 0.05) activeCount++;

        if (f.port === 5001) {
          r1 = mbps;
          document.getElementById('t-h1-rate').innerText = mbps.toFixed(2) + ' Mbps';
          document.getElementById('t-h1-loss').innerText = loss.toFixed(1) + '%';
          document.getElementById('t-h1-lat').innerText = lat.toFixed(1) + ' ms';
        } else if (f.port === 5002) {
          r2 = mbps;
          document.getElementById('t-h2-rate').innerText = mbps.toFixed(2) + ' Mbps';
          document.getElementById('t-h2-loss').innerText = loss.toFixed(1) + '%';
          document.getElementById('t-h2-lat').innerText = lat.toFixed(1) + ' ms';
        } else if (f.port === 5003) {
          r4 = mbps;
          document.getElementById('t-h4-rate').innerText = mbps.toFixed(2) + ' Mbps';
          document.getElementById('t-h4-loss').innerText = loss.toFixed(1) + '%';
          document.getElementById('t-h4-lat').innerText = lat.toFixed(1) + ' ms';
        }
      });

      // Update Chart
      chart.data.datasets[0].data.shift();
      chart.data.datasets[0].data.push(r1);
      chart.data.datasets[1].data.shift();
      chart.data.datasets[1].data.push(r2);
      chart.data.datasets[2].data.shift();
      chart.data.datasets[2].data.push(r4);
      chart.update();

      // Top KPIs
      const total = parseFloat(data.total_mbps) || (r1 + r2 + r4);
      const cap = parseFloat(data.link_capacity_mbps) || 10.0;
      const pct = Math.min(100, Math.round((total / cap) * 100));

      document.getElementById('val-total-rate').innerText = total.toFixed(2);
      document.getElementById('val-load-pct').innerText = pct + '%';
      document.getElementById('bar-total-load').style.width = pct + '%';
      document.getElementById('topo-pipe-fill').style.width = pct + '%';
      document.getElementById('topo-link-text').innerText = `${total.toFixed(2)} Mbps (${pct}% Load)`;
      document.getElementById('val-active-flows').innerHTML = `${activeCount} <span style="font-size: 14px; color: var(--text-dim);">/ 3</span>`;

      // Jain's Fairness Index
      const activeRates = [r1, r2, r4].filter(r => r > 0.05);
      if (activeRates.length > 1) {
        const sum = activeRates.reduce((a, b) => a + b, 0);
        const sumSq = activeRates.reduce((a, b) => a + (b * b), 0);
        const jain = (sum * sum) / (activeRates.length * sumSq);
        document.getElementById('val-jain').innerText = jain.toFixed(3);
        document.getElementById('sub-jain').innerText = "Weighted HTB Contention";
      } else {
        document.getElementById('val-jain').innerText = "1.000";
        document.getElementById('sub-jain').innerText = activeRates.length === 1 ? "Single Flow Saturation" : "Idle / No Contention";
      }

      // Auto-detect phase from telemetry crossover
      if (r4 > 4.5 && r1 < 2.5 && currentPhase !== 2) {
        setPhaseUI(2);
      } else if (r1 > 4.5 && r4 < 2.5 && currentPhase !== 1) {
        setPhaseUI(1);
      }

    } catch (e) {
      console.error("Stats poll error:", e);
    }
  }

  // Polling loop: Logs
  let lastLogCount = 0;
  async function fetchLogs() {
    try {
      const resp = await fetch('/api/logs');
      if (!resp.ok) return;
      const data = await resp.json();
      const logs = data.logs || [];

      if (logs.length !== lastLogCount) {
        lastLogCount = logs.length;
        const box = document.getElementById('log-box');
        if (logs.length === 0) {
          box.innerHTML = '<div class="log-line" style="color: #64748b;">Ready. Start traffic to stream controller events.</div>';
          return;
        }

        box.innerHTML = logs.map(line => {
          let cls = 'log-line';
          if (line.includes('SWAP') || line.includes('Phase 2') || line.includes('Demoted') || line.includes('Promoted')) {
            cls += ' log-dyn';
          } else if (line.includes('OVS') || line.includes('Queue') || line.includes('FLOWMOD')) {
            cls += ' log-ovs';
          } else if (line.includes('SOCKET') || line.includes('UDP')) {
            cls += ' log-soc';
          }
          return `<div class="${cls}">${line}</div>`;
        }).join('');
        box.scrollTop = box.scrollHeight;

        // Auto-detect swap from controller logs
        const hasSwap = logs.some(l => l.includes('SDN POLICY SWAP') || l.includes('Queue 2 promoted'));
        if (hasSwap && currentPhase !== 2) {
          setPhaseUI(2);
        }
      }
    } catch (e) {
      console.error("Logs poll error:", e);
    }
  }

  setInterval(fetchStats, 1000);
  setInterval(fetchLogs, 1500);
</script>

</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    """Zero-dependency HTTP Handler for Dashboard Web UI and REST API."""

    def do_POST(self):
        global PENDING_COMMAND
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""

        if self.path in ("/api/action/start", "/api/action/custom"):
            zero_stats = {
                "timestamp": time.time(),
                "total_mbps": 0.0,
                "link_capacity_mbps": 10.0,
                "flows": [
                    {"port": 5001, "class_name": "Host h1 (Queue 0)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0},
                    {"port": 5002, "class_name": "Host h2 (Queue 1)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0},
                    {"port": 5003, "class_name": "Host h4 (Queue 2)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0}
                ]
            }
            try:
                with open(STATS_FILE, "w") as f:
                    json.dump(zero_stats, f)
                try:
                    os.chmod(STATS_FILE, 0o666)
                except Exception:
                    pass
            except Exception:
                pass

            cmd_str = body.strip() if body.strip() else "START"
            PENDING_COMMAND = cmd_str

            try:
                with open(CMD_FILE, "w") as f:
                    f.write(cmd_str + "\n")
                try:
                    os.chmod(CMD_FILE, 0o666)
                except Exception:
                    pass
            except Exception:
                pass

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status": "start_sent"}')

        elif self.path == "/api/action/stop":
            PENDING_COMMAND = "STOP"
            try:
                with open(CMD_FILE, "w") as f:
                    f.write("STOP\n")
                try:
                    os.chmod(CMD_FILE, 0o666)
                except Exception:
                    pass
            except Exception:
                pass

            zero_stats = {
                "timestamp": time.time(),
                "total_mbps": 0.0,
                "link_capacity_mbps": 10.0,
                "flows": [
                    {"port": 5001, "class_name": "Host h1 (Queue 0)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0},
                    {"port": 5002, "class_name": "Host h2 (Queue 1)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0},
                    {"port": 5003, "class_name": "Host h4 (Queue 2)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0}
                ]
            }
            try:
                with open(STATS_FILE, "w") as f:
                    json.dump(zero_stats, f)
                try:
                    os.chmod(STATS_FILE, 0o666)
                except Exception:
                    pass
            except Exception:
                pass

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status": "stop_sent"}')

        elif self.path == "/api/logs/clear":
            try:
                with open(LOGS_FILE, "w") as f:
                    f.truncate(0)
                try:
                    os.chmod(LOGS_FILE, 0o666)
                except Exception:
                    pass
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status": "logs_cleared"}')

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        global PENDING_COMMAND
        if self.path in ("/", "") or self.path.startswith("/?"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))

        elif self.path == "/api/cmd/pending":
            cmd = PENDING_COMMAND
            PENDING_COMMAND = None
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"cmd": cmd}).encode("utf-8"))

        elif self.path == "/api/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = {}
            if os.path.exists(STATS_FILE):
                for _ in range(3):
                    try:
                        with open(STATS_FILE, "r") as f:
                            raw = f.read()
                        if raw.strip():
                            data = json.loads(raw)
                            break
                    except Exception:
                        time.sleep(0.04)

            file_time = data.get("timestamp", 0)
            now = time.time()
            is_stale = (file_time > 1_000_000) and (now - file_time > 10.0)
            if not data or is_stale:
                data = {
                    "timestamp": now,
                    "total_mbps": 0.0,
                    "link_capacity_mbps": 10.0,
                    "flows": [
                        {"port": 5001, "class_name": "Host h1 (Queue 0)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0},
                        {"port": 5002, "class_name": "Host h2 (Queue 1)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0},
                        {"port": 5003, "class_name": "Host h4 (Queue 2)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0}
                    ]
                }
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path == "/api/logs":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            lines = []
            if os.path.exists(LOGS_FILE):
                try:
                    with open(LOGS_FILE, "r") as f:
                        lines = [line.strip() for line in f.readlines() if line.strip()]
                except Exception:
                    lines = []
            self.wfile.write(json.dumps({"logs": lines}).encode("utf-8"))

        elif self.path == "/api/debug":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            debug = {"stats_file": STATS_FILE, "now": time.time(), "file_exists": os.path.exists(STATS_FILE)}
            self.wfile.write(json.dumps(debug, indent=2).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def main():
    parser = argparse.ArgumentParser(description="SDN QoS Traffic Management Minimal Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="Port to run web server on (default: 8080)")
    args = parser.parse_args()

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print("=" * 60)
    print("      SDN QoS MINIMALIST REAL-TIME DASHBOARD")
    print("=" * 60)
    print(f"  Listening on http://localhost:{args.port}")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
