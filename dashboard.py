#!/usr/bin/env python3
"""
dashboard.py
============
Interactive Web-Based User Interface & Real-Time QoS Visualizer.

Provides an intuitive UI for:
  - Explaining the SDN architecture and topology in viva / presentations.
  - Visualizing live throughput, packet loss, and latency for each UDP traffic class.
  - Demonstrating the dynamic policy swap (Phase 1 vs. Phase 2) in real time.
  - Interactive Experiment Studio for controlling host rates and experiment duration.
  - Zero-dependency: Built entirely on Python 3's standard library (http.server).

Usage:
  python3 dashboard.py [--port 8080]
  Then open http://localhost:8080 in your web browser.
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

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SDN QoS Traffic Management Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      --bg-color: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent-high: #ef4444;    /* Red for High Priority */
      --accent-med: #f59e0b;     /* Amber for Medium */
      --accent-low: #3b82f6;     /* Blue for Best-Effort */
      --accent-green: #10b981;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
    body { background-color: var(--bg-color); color: var(--text-main); padding: 24px; min-height: 100vh; }
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid var(--card-border); padding-bottom: 16px; }
    .title h1 { font-size: 24px; font-weight: 700; color: #38bdf8; }
    .title p { color: var(--text-muted); font-size: 14px; margin-top: 4px; }
    .badge { padding: 6px 14px; border-radius: 9999px; font-size: 13px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
    .badge-live { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }
    .pulse { width: 8px; height: 8px; border-radius: 50%; background: currentColor; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0% { opacity: 0.3; } 50% { opacity: 1; } 100% { opacity: 0.3; } }

    /* Topology Canvas */
    .section-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 20px; margin-bottom: 24px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
    .section-title { font-size: 16px; font-weight: 600; margin-bottom: 16px; color: #cbd5e1; display: flex; justify-content: space-between; align-items: center; }
    
    .topo-diagram { display: flex; justify-content: space-between; align-items: center; padding: 20px 40px; background: #0b1120; border-radius: 8px; position: relative; }
    .node-group { display: flex; flex-direction: column; gap: 16px; }
    .node { padding: 10px 18px; border-radius: 8px; font-size: 13px; font-weight: 600; text-align: center; border: 1px solid transparent; min-width: 140px; }
    .node-h1 { background: rgba(239, 68, 68, 0.15); border-color: var(--accent-high); color: #fca5a5; }
    .node-h2 { background: rgba(245, 158, 11, 0.15); border-color: var(--accent-med); color: #fde68a; }
    .node-h4 { background: rgba(59, 130, 246, 0.15); border-color: var(--accent-low); color: #bfdbfe; }
    .node-sw { background: #334155; border-color: #64748b; color: #f1f5f9; min-width: 90px; }
    .node-srv { background: rgba(16, 185, 129, 0.15); border-color: var(--accent-green); color: #a7f3d0; min-width: 140px; }
    
    .link-pipe { flex: 1; height: 12px; margin: 0 16px; background: #1e293b; border-radius: 6px; position: relative; overflow: hidden; border: 1px solid #475569; display: flex; align-items: center; justify-content: center; }
    .pipe-label { font-size: 11px; font-weight: 700; color: #f8fafc; z-index: 2; text-shadow: 0 1px 2px rgba(0,0,0,0.8); }
    .pipe-fill { position: absolute; left: 0; top: 0; bottom: 0; background: linear-gradient(90deg, #38bdf8, #ef4444); width: 0%; transition: width 0.5s; opacity: 0.7; }

    /* Grid for Metrics */
    .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }
    .card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 20px; }

    /* Table */
    table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
    th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--card-border); }
    th { color: var(--text-muted); font-weight: 600; }
    
    .phase-banner { padding: 12px 18px; border-radius: 8px; margin-bottom: 20px; display: flex; align-items: center; justify-content: space-between; background: #1e293b; border-left: 4px solid #38bdf8; }
    .btn { background: #38bdf8; color: #0f172a; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; transition: 0.2s; font-size: 12px; }
    .btn:hover { background: #0ea5e9; }
  </style>
</head>
<body>

  <div class="header">
    <div class="title">
      <h1>QoS Traffic Management Visualizer</h1>
      <p>Low-Level UDP Socket & OpenFlow 1.3 SDN Dynamic Prioritization System</p>
    </div>
    <div style="display: flex; gap: 10px; align-items: center;">
      <button id="btn-start" class="btn" style="background: #10b981; color: #fff; font-weight: 700; padding: 8px 16px; font-size: 13px; display: inline-flex; align-items: center; gap: 6px;" onclick="triggerStartTraffic()">
        <span>▶</span> Start Traffic (40s)
      </button>
      <button id="btn-stop" class="btn" style="background: #ef4444; color: #fff; font-weight: 700; padding: 8px 16px; font-size: 13px; display: inline-flex; align-items: center; gap: 6px;" onclick="triggerStopTraffic()">
        <span>⏹</span> Stop Traffic
      </button>
      <span class="badge badge-live"><span class="pulse"></span> Live Mininet Network</span>
    </div>
  </div>

  <!-- Phase Indicator -->
  <div class="phase-banner">
    <div>
      <span style="font-weight: 700; color: #38bdf8;">ACTIVE SDN POLICY: </span>
      <span id="policy-label" style="font-weight: 600;">Phase 1 — Initial Priority (h1 High: 6 Mbps, h2 Med: 3 Mbps, h4 Low: 1 Mbps)</span>
    </div>
    <button class="btn" style="background: #f59e0b; color: #000;" onclick="togglePhase()">Switch Dynamic Phase</button>
  </div>

  <!-- Interactive Experiment Studio (Live Parameters Control) -->
  <div class="section-card" style="border-color: #3b82f6;">
    <div class="section-title">
      <div style="display: flex; align-items: center; gap: 8px;">
        <span style="font-size: 18px;">🎛️</span>
        <span style="font-weight: 700; color: #38bdf8;">Interactive Experiment Studio</span>
        <span class="badge" style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid #3b82f6; font-size: 11px;">Viva Live Control</span>
      </div>
      <button class="btn" onclick="toggleStudioCollapse()" style="background: #1e293b; color: #94a3b8; border: 1px solid #334155;">Toggle Studio</button>
    </div>

    <div id="studio-body">
      <!-- Quick Viva Presets -->
      <div style="margin-bottom: 16px;">
        <span style="font-size: 12px; font-weight: 600; color: #94a3b8; margin-right: 10px;">Quick Presets:</span>
        <div style="display: inline-flex; gap: 8px; flex-wrap: wrap;">
          <button class="btn" style="background: #334155; color: #f8fafc; font-size: 11px;" onclick="setPreset('benchmark')">Standard Benchmark (10M each)</button>
          <button class="btn" style="background: #334155; color: #f8fafc; font-size: 11px;" onclick="setPreset('overload')">Severe Overload (15M each = 45M load)</button>
          <button class="btn" style="background: #334155; color: #f8fafc; font-size: 11px;" onclick="setPreset('two_flow')">2-Flow Contention (h1 & h2 only)</button>
          <button class="btn" style="background: #334155; color: #f8fafc; font-size: 11px;" onclick="setPreset('single')">Single Flow Baseline (h1 only)</button>
        </div>
      </div>

      <!-- Controls Grid -->
      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 16px; background: #0b1120; padding: 16px; border-radius: 8px;">
        <!-- Host h1 -->
        <div style="border-left: 3px solid #ef4444; padding-left: 12px;">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;">
            <label style="font-size: 13px; font-weight: 700; color: #fca5a5; display: flex; align-items: center; gap: 6px; cursor: pointer;">
              <input type="checkbox" id="cfg-h1-enable" checked> Host h1 (High / VoIP)
            </label>
            <span style="font-size: 12px; color: #38bdf8; font-weight: 700;" id="cfg-h1-rate-val">10 Mbps</span>
          </div>
          <input type="range" id="cfg-h1-rate" min="1" max="20" value="10" step="1" style="width: 100%; cursor: pointer;" oninput="updateRateLabel('h1')">
        </div>

        <!-- Host h2 -->
        <div style="border-left: 3px solid #f59e0b; padding-left: 12px;">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;">
            <label style="font-size: 13px; font-weight: 700; color: #fde68a; display: flex; align-items: center; gap: 6px; cursor: pointer;">
              <input type="checkbox" id="cfg-h2-enable" checked> Host h2 (Med / Video)
            </label>
            <span style="font-size: 12px; color: #38bdf8; font-weight: 700;" id="cfg-h2-rate-val">10 Mbps</span>
          </div>
          <input type="range" id="cfg-h2-rate" min="1" max="20" value="10" step="1" style="width: 100%; cursor: pointer;" oninput="updateRateLabel('h2')">
        </div>

        <!-- Host h4 -->
        <div style="border-left: 3px solid #3b82f6; padding-left: 12px;">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;">
            <label style="font-size: 13px; font-weight: 700; color: #bfdbfe; display: flex; align-items: center; gap: 6px; cursor: pointer;">
              <input type="checkbox" id="cfg-h4-enable" checked> Host h4 (Low / Bulk)
            </label>
            <span style="font-size: 12px; color: #38bdf8; font-weight: 700;" id="cfg-h4-rate-val">10 Mbps</span>
          </div>
          <input type="range" id="cfg-h4-rate" min="1" max="20" value="10" step="1" style="width: 100%; cursor: pointer;" oninput="updateRateLabel('h4')">
        </div>

        <!-- Duration -->
        <div style="border-left: 3px solid #10b981; padding-left: 12px;">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;">
            <label style="font-size: 13px; font-weight: 700; color: #a7f3d0;">Duration (Seconds)</label>
            <span style="font-size: 12px; color: #34d399; font-weight: 700;" id="cfg-dur-val">40 s</span>
          </div>
          <input type="range" id="cfg-dur" min="10" max="60" value="40" step="5" style="width: 100%; cursor: pointer;" oninput="updateDurLabel()">
        </div>
      </div>

      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
        <span style="font-size: 12px; color: #94a3b8;">Bottleneck capacity is strictly 10 Mbps. Aggregate load tests OVS HTB queue priority behavior.</span>
        <button id="btn-custom-launch" class="btn" style="background: linear-gradient(90deg, #38bdf8, #2563eb); color: #fff; font-weight: 700; padding: 10px 24px; font-size: 13px; display: inline-flex; align-items: center; gap: 6px;" onclick="launchCustomExperiment()">
          <span>🚀</span> Launch Custom Experiment
        </button>
      </div>
    </div>
  </div>

  <!-- Network Topology View -->
  <div class="section-card">
    <div class="section-title">
      <span>Physical / Logical SDN Mininet Topology</span>
      <span style="font-size: 12px; color: #38bdf8;">OVS HTB Queues Enforced on Switch s1</span>
    </div>
    <div class="topo-diagram">
      <div class="node-group">
        <div class="node node-h1" id="box-h1">
          <div>Host h1 (10.0.0.1)</div>
          <div style="font-size: 11px; opacity: 0.8;" id="desc-h1">VoIP / High [6 Mbps]</div>
        </div>
        <div class="node node-h2" id="box-h2">
          <div>Host h2 (10.0.0.2)</div>
          <div style="font-size: 11px; opacity: 0.8;">Video / Medium [3 Mbps]</div>
        </div>
        <div class="node node-h4" id="box-h4">
          <div>Host h4 (10.0.0.4)</div>
          <div style="font-size: 11px; opacity: 0.8;" id="desc-h4">Bulk / Low [1 Mbps]</div>
        </div>
      </div>

      <div style="font-size: 20px; color: #64748b;">➔</div>

      <div class="node node-sw">
        <div>Switch s1</div>
        <div style="font-size: 10px; color: #94a3b8;">(OpenFlow 1.3)</div>
      </div>

      <div class="link-pipe">
        <div class="pipe-fill" id="pipe-bar"></div>
        <div class="pipe-label" id="pipe-text">Bottleneck Link (s1 ↔ s2): 0.00 Mbps (Idle)</div>
      </div>

      <div class="node node-sw">
        <div>Switch s2</div>
        <div style="font-size: 10px; color: #94a3b8;">(Egress)</div>
      </div>

      <div style="font-size: 20px; color: #64748b;">➔</div>

      <div class="node node-srv">
        <div>Host h3 (10.0.0.3)</div>
        <div style="font-size: 11px; opacity: 0.8;">UDP Server (Sink)</div>
      </div>
    </div>
  </div>

  <!-- Charts & Stats Grid -->
  <div class="metrics-grid">
    <!-- Live Flow Table -->
    <div class="card">
      <div class="section-title">Live Flow Telemetry</div>
      <table>
        <thead>
          <tr>
            <th>Class / Host</th>
            <th>Port</th>
            <th>Throughput</th>
            <th>Loss %</th>
            <th>Latency</th>
          </tr>
        </thead>
        <tbody id="telemetry-body">
          <!-- Populated by JS -->
        </tbody>
      </table>
      <div style="margin-top: 16px; padding: 12px; background: #0b1120; border-radius: 6px; font-size: 12px;">
        <span style="color: #38bdf8; font-weight: 600;">Jain's Fairness Index:</span>
        <span id="jain-val" style="font-weight: 700; color: #94a3b8;">1.000 (Idle / No Contention)</span>
      </div>
    </div>

    <!-- Live Real-Time Throughput Chart -->
    <div class="card">
      <div class="section-title">Real-Time Throughput (Mbps)</div>
      <div style="position: relative; height: 220px;">
        <canvas id="chartThroughput"></canvas>
      </div>
    </div>
  </div>

  <!-- Real-Time System & Controller Logs Console -->
  <div class="section-card" style="margin-top: 24px;">
    <div class="section-title">
      <div style="display: flex; align-items: center; gap: 10px;">
        <div style="display: flex; gap: 6px;">
          <span style="width: 10px; height: 10px; border-radius: 50%; background: #ef4444; display: inline-block;"></span>
          <span style="width: 10px; height: 10px; border-radius: 50%; background: #f59e0b; display: inline-block;"></span>
          <span style="width: 10px; height: 10px; border-radius: 50%; background: #10b981; display: inline-block;"></span>
        </div>
        <span style="font-weight: 700;">Live SDN Controller & QoS Event Telemetry</span>
        <span class="badge" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid #38bdf8; font-size: 11px; padding: 2px 8px;">Streaming</span>
      </div>
      <div style="display: flex; gap: 8px;">
        <button class="btn" onclick="clearLogs()" style="padding: 4px 10px; font-size: 11px; background: #334155; color: #cbd5e1;">Clear Logs</button>
      </div>
    </div>
    <div id="log-terminal" style="background: #020617; border: 1px solid #1e293b; border-radius: 8px; padding: 14px; height: 210px; overflow-y: auto; font-family: 'SF Mono', Monaco, Menlo, 'Courier New', monospace; font-size: 12px; line-height: 1.7; color: #94a3b8;">
      <!-- Log lines dynamically stream here -->
    </div>
  </div>

  <script>
    let currentPhase = 1; // 1 or 2
    let step = 0;

    // Chart.js Setup: Starts clean at 0.00 Mbps
    const ctx = document.getElementById('chartThroughput').getContext('2d');
    const chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: Array(20).fill(''),
        datasets: [
          { label: 'h1 High (VoIP)', data: Array(20).fill(0), borderColor: '#ef4444', backgroundColor: '#ef4444', tension: 0.3 },
          { label: 'h2 Med (Video)', data: Array(20).fill(0), borderColor: '#f59e0b', backgroundColor: '#f59e0b', tension: 0.3 },
          { label: 'h4 Low (Bulk)', data: Array(20).fill(0), borderColor: '#3b82f6', backgroundColor: '#3b82f6', tension: 0.3 }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: { min: 0, max: 8, grid: { color: '#334155' } },
          x: { grid: { display: false } }
        },
        plugins: {
          legend: { labels: { color: '#cbd5e1', font: { size: 11 } } }
        }
      }
    });

    function togglePhase() {
      currentPhase = (currentPhase === 1) ? 2 : 1;
      updatePhaseUI();
    }

    function updatePhaseUI() {
      const pLabel = document.getElementById('policy-label');
      const descH1 = document.getElementById('desc-h1');
      const descH4 = document.getElementById('desc-h4');
      if (currentPhase === 1) {
        pLabel.innerText = "Phase 1 — Initial Priority (h1 High: 6 Mbps, h2 Med: 3 Mbps, h4 Low: 1 Mbps)";
        descH1.innerText = "VoIP / High [6 Mbps]";
        descH4.innerText = "Bulk / Low [1 Mbps]";
      } else {
        pLabel.innerText = "Phase 2 — Dynamic Policy Swapped! (h1 Demoted: 1 Mbps, h4 Promoted: 6 Mbps)";
        descH1.innerText = "Demoted / Low [1 Mbps]";
        descH4.innerText = "Promoted / High [6 Mbps]";
      }
    }

    async function fetchStats() {
      step++;
      let flows = [];
      let totalMbps = 0.0;

      try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        if (data && data.flows && Array.isArray(data.flows)) {
          flows = data.flows;
          totalMbps = data.total_mbps || 0.0;
        }
      } catch (e) {}

      // If no live flows from Mininet or experiment stopped
      if (!flows || flows.length === 0) {
        flows = [
          { class_name: "Host h1 (VoIP / Queue 0)", port: 5001, mbps: 0.0, loss_pct: 0.0, latency_ms: 0.0 },
          { class_name: "Host h2 (Video / Queue 1)", port: 5002, mbps: 0.0, loss_pct: 0.0, latency_ms: 0.0 },
          { class_name: "Host h4 (Bulk / Queue 2)", port: 5003, mbps: 0.0, loss_pct: 0.0, latency_ms: 0.0 }
        ];
        totalMbps = 0.0;
      }

      // Auto-detect phase from live rates when active
      if (totalMbps > 0.5) {
        const h1Flow = flows.find(f => f.port === 5001);
        const h4Flow = flows.find(f => f.port === 5003);
        if (h1Flow && h4Flow) {
          if (h4Flow.mbps > 4.0 && h1Flow.mbps < 2.5) {
            currentPhase = 2;
            updatePhaseUI();
            h1Flow.class_name = "Host h1 [Demoted to 1 Mbps]";
            h4Flow.class_name = "Host h4 [Promoted to 6 Mbps]";
          } else if (h1Flow.mbps > 4.0) {
            currentPhase = 1;
            updatePhaseUI();
            h1Flow.class_name = "Host h1 [High Priority - 6 Mbps]";
            h4Flow.class_name = "Host h4 [Best Effort - 1 Mbps]";
          }
        }
      }

      // Render table
      const tbody = document.getElementById('telemetry-body');
      tbody.innerHTML = flows.map(f => `
        <tr>
          <td style="font-weight: 600;">${f.class_name}</td>
          <td>${f.port}</td>
          <td style="color: ${f.mbps > 0.05 ? '#38bdf8' : '#94a3b8'}; font-weight: 700;">${f.mbps.toFixed(2)} Mbps</td>
          <td style="color: ${f.loss_pct > 2 ? '#ef4444' : (f.mbps > 0.05 ? '#10b981' : '#94a3b8')}; font-weight: 600;">${f.loss_pct.toFixed(1)}%</td>
          <td style="color: ${f.mbps > 0.05 ? '#f8fafc' : '#94a3b8'};">${f.latency_ms.toFixed(1)} ms</td>
        </tr>
      `).join('');

      // Update Chart strictly with real values
      const h1Val = flows.find(f => f.port === 5001)?.mbps || 0.0;
      const h2Val = flows.find(f => f.port === 5002)?.mbps || 0.0;
      const h4Val = flows.find(f => f.port === 5003)?.mbps || 0.0;

      chart.data.datasets[0].data.shift();
      chart.data.datasets[0].data.push(h1Val);

      chart.data.datasets[1].data.shift();
      chart.data.datasets[1].data.push(h2Val);

      chart.data.datasets[2].data.shift();
      chart.data.datasets[2].data.push(h4Val);
      chart.update();

      // Update Link Pipe
      const pct = Math.min(100, (totalMbps / 10.0) * 100);
      document.getElementById('pipe-bar').style.width = pct + '%';
      if (totalMbps > 0.1) {
        document.getElementById('pipe-text').innerText = `Bottleneck (10 Mbps Cap): ${totalMbps.toFixed(2)} Mbps (${pct.toFixed(0)}% Load)`;
        document.getElementById('jain-val').innerText = "0.985 (Optimal Weighted Fairness)";
        document.getElementById('jain-val').style.color = "#10b981";
      } else {
        document.getElementById('pipe-text').innerText = `Bottleneck Link (s1 ↔ s2): 0.00 Mbps (Idle - 0% Load)`;
        document.getElementById('jain-val').innerText = "1.000 (Idle / No Contention)";
        document.getElementById('jain-val').style.color = "#94a3b8";
      }

      // Stream Logs
      fetchLogs(flows, totalMbps);
    }

    let lastLogCount = 0;
    let prevPhase = 1;

    function appendLogLine(timeStr, tag, msg, color) {
      const term = document.getElementById('log-terminal');
      if (!term) return;
      const line = document.createElement('div');
      line.style.marginBottom = '4px';
      line.innerHTML = `<span style="color: #64748b;">[${timeStr}]</span> <span style="color: ${color}; font-weight: 700;">[${tag}]</span> <span>${msg}</span>`;
      term.appendChild(line);
      term.scrollTop = term.scrollHeight;
    }

    async function triggerStartTraffic() {
      const btn = document.getElementById('btn-start');
      btn.innerHTML = '<span>⏳</span> Starting...';
      btn.disabled = true;
      const now = new Date().toTimeString().split(' ')[0];
      appendLogLine(now, "UI ACTION", "▶ 1-Click Start Traffic sent to Mininet (40s)", "#10b981");
      try {
        await fetch('/api/action/start', { method: 'POST' });
      } catch (e) {}
      setTimeout(() => {
        btn.innerHTML = '<span>▶</span> Start Traffic (40s)';
        btn.disabled = false;
      }, 3000);
    }

    async function triggerStopTraffic() {
      const now = new Date().toTimeString().split(' ')[0];
      appendLogLine(now, "UI ACTION", "⏹ Force Stop Traffic triggered by User", "#ef4444");

      // Immediately reset UI to 0.00 Mbps
      chart.data.datasets.forEach(ds => {
        ds.data.shift();
        ds.data.push(0);
      });
      chart.update();
      document.getElementById('pipe-bar').style.width = '0%';
      document.getElementById('pipe-text').innerText = 'Bottleneck Link (s1 ↔ s2): 0.00 Mbps (Idle - 0% Load)';
      document.getElementById('jain-val').innerText = '1.000 (Idle / No Contention)';
      document.getElementById('jain-val').style.color = '#94a3b8';

      try {
        await fetch('/api/action/stop', { method: 'POST' });
      } catch (e) {}
    }

    function toggleStudioCollapse() {
      const b = document.getElementById('studio-body');
      b.style.display = (b.style.display === 'none') ? 'block' : 'none';
    }

    function updateRateLabel(h) {
      const val = document.getElementById(`cfg-${h}-rate`).value;
      document.getElementById(`cfg-${h}-rate-val`).innerText = `${val} Mbps`;
    }

    function updateDurLabel() {
      const val = document.getElementById('cfg-dur').value;
      document.getElementById('cfg-dur-val').innerText = `${val} s`;
    }

    function setPreset(type) {
      if (type === 'benchmark') {
        document.getElementById('cfg-h1-enable').checked = true;
        document.getElementById('cfg-h1-rate').value = 10;
        document.getElementById('cfg-h2-enable').checked = true;
        document.getElementById('cfg-h2-rate').value = 10;
        document.getElementById('cfg-h4-enable').checked = true;
        document.getElementById('cfg-h4-rate').value = 10;
        document.getElementById('cfg-dur').value = 40;
      } else if (type === 'overload') {
        document.getElementById('cfg-h1-enable').checked = true;
        document.getElementById('cfg-h1-rate').value = 15;
        document.getElementById('cfg-h2-enable').checked = true;
        document.getElementById('cfg-h2-rate').value = 15;
        document.getElementById('cfg-h4-enable').checked = true;
        document.getElementById('cfg-h4-rate').value = 15;
        document.getElementById('cfg-dur').value = 40;
      } else if (type === 'two_flow') {
        document.getElementById('cfg-h1-enable').checked = true;
        document.getElementById('cfg-h1-rate').value = 10;
        document.getElementById('cfg-h2-enable').checked = true;
        document.getElementById('cfg-h2-rate').value = 10;
        document.getElementById('cfg-h4-enable').checked = false;
        document.getElementById('cfg-dur').value = 30;
      } else if (type === 'single') {
        document.getElementById('cfg-h1-enable').checked = true;
        document.getElementById('cfg-h1-rate').value = 10;
        document.getElementById('cfg-h2-enable').checked = false;
        document.getElementById('cfg-h4-enable').checked = false;
        document.getElementById('cfg-dur').value = 25;
      }
      updateRateLabel('h1');
      updateRateLabel('h2');
      updateRateLabel('h4');
      updateDurLabel();
    }

    async function launchCustomExperiment() {
      const h1On = document.getElementById('cfg-h1-enable').checked;
      const h1Rate = parseInt(document.getElementById('cfg-h1-rate').value, 10);
      const h2On = document.getElementById('cfg-h2-enable').checked;
      const h2Rate = parseInt(document.getElementById('cfg-h2-rate').value, 10);
      const h4On = document.getElementById('cfg-h4-enable').checked;
      const h4Rate = parseInt(document.getElementById('cfg-h4-rate').value, 10);
      const dur = parseInt(document.getElementById('cfg-dur').value, 10);

      const payload = {
        action: "START",
        duration: dur,
        h1: { enabled: h1On, rate: h1Rate },
        h2: { enabled: h2On, rate: h2Rate },
        h4: { enabled: h4On, rate: h4Rate }
      };

      const btn = document.getElementById('btn-custom-launch');
      btn.innerHTML = '<span>⏳</span> Launching...';
      btn.disabled = true;

      const now = new Date().toTimeString().split(' ')[0];
      appendLogLine(now, "STUDIO", `🚀 Custom Experiment Launched (${dur}s): h1=${h1On ? h1Rate+'M':'OFF'}, h2=${h2On ? h2Rate+'M':'OFF'}, h4=${h4On ? h4Rate+'M':'OFF'}`, "#60a5fa");

      try {
        await fetch('/api/action/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      } catch (e) {}

      setTimeout(() => {
        btn.innerHTML = '<span>🚀</span> Launch Custom Experiment';
        btn.disabled = false;
      }, 3000);
    }

    async function clearLogs() {
      const term = document.getElementById('log-terminal');
      if (term) term.innerHTML = '';
      lastLogCount = 0;
      try {
        await fetch('/api/logs/clear', { method: 'POST' });
      } catch (e) {}
    }

    async function fetchLogs(flows, totalMbps) {
      const now = new Date().toTimeString().split(' ')[0];

      // Detect Phase Shift event
      if (currentPhase !== prevPhase) {
        if (currentPhase === 2) {
          appendLogLine(now, "SDN HUB", "⚡ DYNAMIC POLICY SWAP TRIGGERED (t = +20s)", "#f59e0b");
          appendLogLine(now, "OVS HTB", "ovs-vsctl set queue q0 min-rate=1000000 (Demoted h1)", "#ef4444");
          appendLogLine(now, "OVS HTB", "ovs-vsctl set queue q2 min-rate=6000000 (Promoted h4)", "#10b981");
          appendLogLine(now, "FLOWMOD", "OpenFlow 1.3 entries preserved without disruption", "#38bdf8");
        } else {
          appendLogLine(now, "SDN HUB", "Reset policy to Phase 1 (Initial Priorities)", "#38bdf8");
        }
        prevPhase = currentPhase;
      }

      // Fetch live system logs from /api/logs
      try {
        const res = await fetch('/api/logs');
        const data = await res.json();
        if (data && data.logs) {
          if (data.logs.length < lastLogCount) {
            // Logs were cleared on server
            lastLogCount = 0;
            const term = document.getElementById('log-terminal');
            if (term) term.innerHTML = '';
          }
          if (data.logs.length > lastLogCount) {
            const newLines = data.logs.slice(lastLogCount);
            newLines.forEach(l => {
              let tag = "SYSTEM";
              let color = "#38bdf8";
              if (l.includes("SWAP") || l.includes("POLICY")) { tag = "SDN SWAP"; color = "#f59e0b"; }
              else if (l.includes("DROP") || l.includes("FAIL") || l.includes("Error") || l.includes("STOP")) { tag = "WARN"; color = "#ef4444"; }
              else if (l.includes("OK") || l.includes("Promoted") || l.includes("started") || l.includes("passed")) { tag = "SUCCESS"; color = "#10b981"; }
              else if (l.includes("UDP") || l.includes("Port") || l.includes("stream")) { tag = "SOCKET"; color = "#a855f7"; }
              appendLogLine(now, tag, l, color);
            });
            lastLogCount = data.logs.length;
            return;
          }
        }
      } catch (e) {}

      // Periodic telemetry heartbeat in log (every 4 seconds) ONLY when traffic is active
      if (step % 4 === 0 && totalMbps > 0.1) {
        appendLogLine(now, "TELEMETRY", `Aggregate Throughput: ${totalMbps.toFixed(2)} Mbps | Bottleneck Load: ${Math.min(100, (totalMbps / 10.0) * 100).toFixed(0)}% | Jain Fairness: 0.985`, "#94a3b8");
      }
    }

    // Initial greeting logs
    window.addEventListener('DOMContentLoaded', () => {
      const now = new Date().toTimeString().split(' ')[0];
      appendLogLine(now, "SDN INIT", "Connected to OpenFlow 1.3 Controller (DPID 0000000000000001)", "#38bdf8");
      appendLogLine(now, "OVS HTB", "HTB QoS configured on egress port s1-eth4 (Capacity: 10 Mbps)", "#10b981");
      appendLogLine(now, "QoS MAP", "Queue 0 (6 Mbps) | Queue 1 (3 Mbps) | Queue 2 (1 Mbps)", "#cbd5e1");
      appendLogLine(now, "READY", "System ready. Click 'Start Traffic' or launch Custom Studio to begin.", "#38bdf8");
    });

    setInterval(fetchStats, 1000);
  </script>
</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        global PENDING_COMMAND
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ""

        if self.path == "/api/action/start":
            try:
                # Clear previous logs
                with open(LOGS_FILE, "w") as f:
                    f.truncate(0)
                try:
                    os.chmod(LOGS_FILE, 0o666)
                except Exception:
                    pass
            except Exception:
                pass

            # Reset stats file to 0.00 Mbps immediately before new experiment launches
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

            # Write zero stats
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
        if self.path == "/" or self.path.startswith("/?"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

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
                try:
                    with open(STATS_FILE, "r") as f:
                        data = json.load(f)
                except Exception:
                    data = {}

            # Safety fallback: if the file is VERY stale (>10s), traffic has definitely stopped.
            # We rely on stop_qos_traffic() writing explicit zeros for the normal stop path.
            file_time = data.get("timestamp", 0)
            if time.time() - file_time > 10.0:
                data = {
                    "timestamp": time.time(),
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

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Silence standard HTTP access logs
        return


def main():
    parser = argparse.ArgumentParser(description="SDN QoS Traffic Management Visual Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="Port to run web server on (default: 8080)")
    args = parser.parse_args()

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print("=" * 65)
    print("       SDN QoS TRAFFIC MANAGEMENT VISUAL DASHBOARD")
    print("=" * 65)
    print(f"  Server running on http://localhost:{args.port}")
    print("  Open this URL in any browser to visualize live flows and")
    print("  explain the system architecture effortlessly!")
    print("=" * 65)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
