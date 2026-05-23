import os
import re
import subprocess
import time
from flask import Flask, jsonify, render_template_string, request
import psutil

# load static files from root dir
app = Flask(__name__, static_folder=".", static_url_path="")


def run_command(command):
    """Helper to run shell commands safely"""
    try:
        result = subprocess.run(
            command, shell=True, check=True, text=True, capture_output=True
        )
        return {"status": "success", "output": result.stdout}
    except subprocess.CalledProcessError as e:
        return {"status": "error", "output": e.stderr}


def netmask_to_cidr(netmask):
    if not netmask or "." not in str(netmask):
        return str(netmask).replace("/", "") if netmask else "24"
    try:
        parts = [int(x) for x in netmask.split(".")]
        return str("".join([bin(x)[2:].zfill(8) for x in parts]).count("1"))
    except Exception:
        return "24"


def get_interfaces():
    interfaces = []
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    for iface, stat in stats.items():
        ip_addr, netmask = "N/A", "N/A"
        if iface in addrs:
            for addr in addrs[iface]:
                if addr.family == 2:
                    ip_addr, netmask = addr.address, addr.netmask
        interfaces.append(
            {
                "name": iface,
                "is_up": stat.isup,
                "mtu": stat.mtu,
                "speed": stat.speed,
                "ip": ip_addr,
                "netmask": netmask,
            }
        )
    return interfaces


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/interfaces", methods=["GET"])
def api_interfaces():
    return jsonify(get_interfaces())


@app.route("/api/utilization", methods=["GET"])
def api_utilization():
    io_before = psutil.net_io_counters(pernic=True)
    time.sleep(0.5)
    io_after = psutil.net_io_counters(pernic=True)
    utilization = {}
    for iface in io_after:
        if iface in io_before:
            tx_speed = (
                io_after[iface].bytes_sent - io_before[iface].bytes_sent
            ) * 2
            rx_speed = (
                io_after[iface].bytes_recv - io_before[iface].bytes_recv
            ) * 2
            utilization[iface] = {
                "tx_kbps": round(tx_speed / 1024, 2),
                "rx_kbps": round(rx_speed / 1024, 2),
            }
    return jsonify(utilization)


@app.route("/api/configure", methods=["POST"])
def configure_interface():
    data = request.json
    iface = data.get("interface")
    new_ip = data.get("ip", "").strip()
    raw_netmask = data.get("netmask", "").strip()
    mtu = data.get("mtu")
    
    if not iface:
        return jsonify({"status": "error", "output": "No interface specified"})
        
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    current_ip, current_netmask, current_mtu = "N/A", "N/A", None
    
    if iface in addrs:
        for addr in addrs[iface]:
            if addr.family == 2:  # AF_INET (IPv4)
                current_ip = addr.address
                current_netmask = addr.netmask
    if iface in stats:
        current_mtu = stats[iface].mtu

    actions_taken = []
    
    # CASE 1: Both IP and Subnet are empty strings -> Remove the IP completely
    if not new_ip and not raw_netmask:
        if current_ip != "N/A":
            flush_res = run_command(f"sudo ip addr flush dev {iface}")
            if flush_res["status"] == "error":
                return jsonify({"status": "error", "output": f"Failed to remove IP configuration: {flush_res['output']}"})
            actions_taken.append(f"Removed IP configuration from {iface} (interface flushed).")
        else:
            actions_taken.append("Interface already has no IP assigned. Skipping flush.")
            
    # CASE 2: New IP details are provided -> Update or preserve
    elif new_ip:
        new_cidr = netmask_to_cidr(raw_netmask if raw_netmask else "24")
        current_cidr = netmask_to_cidr(current_netmask)
        
        if new_ip == current_ip and new_cidr == current_cidr:
            actions_taken.append("IP/Subnet unchanged. Skipping interface flush.")
        else:
            flush_res = run_command(f"sudo ip addr flush dev {iface}")
            if flush_res["status"] == "error":
                return jsonify({"status": "error", "output": f"Flush failed: {flush_res['output']}"})
            add_res = run_command(f"sudo ip addr add {new_ip}/{new_cidr} dev {iface}")
            if add_res["status"] == "error":
                return jsonify({"status": "error", "output": f"IP assignment failed: {add_res['output']}"})
            actions_taken.append(f"IP updated to {new_ip}/{new_cidr}.")
            
    # CASE 3: Incomplete data input (IP provided without netmask or vice-versa incorrectly)
    else:
        return jsonify({"status": "error", "output": "Incomplete configuration. Provide both IP and subnet, or leave both blank to clear the IP."})

    # Handle MTU adjustments independently
    if mtu:
        try:
            mtu_int = int(mtu)
            if mtu_int != current_mtu:
                mtu_res = run_command(f"sudo ip link set dev {iface} mtu {mtu_int}")
                if mtu_res["status"] == "error":
                    return jsonify({"status": "error", "output": f"MTU adjustment failed: {mtu_res['output']}"})
                actions_taken.append(f"MTU updated to {mtu_int}.")
        except ValueError:
            return jsonify({"status": "error", "output": "Invalid MTU format."})

    return jsonify({
        "status": "success", 
        "output": " ".join(actions_taken) if actions_taken else "No changes required."
    })


@app.route("/api/bridge/create", methods=["POST"])
def create_bridge():
    data = request.json
    bridge_name, interfaces = data.get("bridge_name"), data.get("interfaces", [])
    if not bridge_name:
        return jsonify({"status": "error", "output": "Bridge name required"})
    res = run_command(f"sudo ip link add name {bridge_name} type bridge")
    if res["status"] == "error":
        return jsonify(res)
    run_command(f"sudo ip link set dev {bridge_name} up")
    for iface in interfaces:
        run_command(f"sudo ip link set dev {iface} master {bridge_name}")
    return jsonify(
        {"status": "success", "output": f"Bridge {bridge_name} created."}
    )


@app.route("/api/bridge/delete", methods=["POST"])
def delete_bridge():
    bridge_name = request.json.get("bridge_name")
    run_command(f"sudo ip link set dev {bridge_name} down")
    return jsonify(run_command(f"sudo ip link del dev {bridge_name} type bridge"))


@app.route("/api/netem/status", methods=["GET"])
def get_netem_status():
    iface = request.args.get("interface")
    if not iface:
        return jsonify({"status": "error", "output": "No interface specified"})
    res = run_command(f"tc qdisc show dev {iface}")
    output = res.get("output", "")
    current_config = {
        "delay": 0,
        "jitter": 0,
        "loss": 0.0,
        "corrupt": 0.0,
        "duplicate": 0.0,
        "reorder": 0.0,
    }
    if "netem" in output:
        delay_match = re.search(
            r"delay\s+([0-9.]+)(ms|us)(?:\s+([0-9.]+)(ms|us))?", output
        )
        if delay_match:
            current_config["delay"] = int(float(delay_match.group(1)))
            if delay_match.group(3):
                current_config["jitter"] = int(float(delay_match.group(3)))
        loss_match = re.search(r"loss\s+([0-9.]+)%", output)
        if loss_match:
            current_config["loss"] = float(loss_match.group(1))
        corrupt_match = re.search(r"corrupt\s+([0-9.]+)%", output)
        if corrupt_match:
            current_config["corrupt"] = float(corrupt_match.group(1))
        dup_match = re.search(r"duplicate\s+([0-9.]+)%", output)
        if dup_match:
            current_config["duplicate"] = float(dup_match.group(1))
        reorder_match = re.search(r"reorder\s+([0-9.]+)%", output)
        if reorder_match:
            current_config["reorder"] = float(reorder_match.group(1))
    return jsonify({"status": "success", "config": current_config})


@app.route("/api/netem/apply", methods=["POST"])
def apply_netem():
    data = request.json
    iface = data.get("interface")
    if not iface:
        return jsonify({"status": "error", "output": "No interface selected"})
    run_command(f"sudo tc qdisc del dev {iface} root")
    cmd = f"sudo tc qdisc add dev {iface} root netem"
    delay, jitter = int(data.get("delay", 0)), int(data.get("jitter", 0))
    if delay > 0:
        cmd += f" delay {delay}ms"
        if jitter > 0:
            cmd += f" {jitter}ms"
    for param in ["loss", "corrupt", "duplicate", "reorder"]:
        val = float(data.get(param, 0))
        if val > 0:
            if param == "reorder" and delay == 0:
                cmd += " delay 10ms"
            cmd += f" {param} {val}%"
    if cmd.endswith("netem"):
        return jsonify({"status": "success", "output": "No variables set. Rules cleared."})
    return jsonify(run_command(cmd))


@app.route("/api/netem/clear", methods=["POST"])
def clear_netem():
    iface = request.json.get("interface")
    run_command(f"sudo tc qdisc del dev {iface} root")
    return jsonify({"status": "success", "output": "Emulation rules cleared."})


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Network Interface Manager</title>
    <link rel="stylesheet" href="/tailwind.min.css">
</head>
<body class="bg-gray-900 text-gray-100 font-sans p-6">
    <div class="max-w-6xl mx-auto">
        
        <!-- Header Section with Inline SVG Network Icon -->
        <div class="flex items-center space-x-3 mb-6 border-b border-gray-700 pb-3">
            <svg class="w-8 h-8 text-indigo-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                <path stroke-linecap="round" stroke-linejoin="round" d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064"></path>
            </svg>
            <h1 class="text-3xl font-bold text-indigo-400">Network Interface Dashboard</h1>
        </div>
        
        <!-- Interfaces Monitor Card -->
        <div class="bg-gray-800 rounded-lg shadow-md p-6 mb-8">
            <h2 class="text-xl font-semibold mb-4 text-gray-300">Available Network Cards</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="border-b border-gray-700 text-gray-400">
                            <th class="p-3">Interface</th>
                            <th class="p-3">Status</th>
                            <th class="p-3">IP Address</th>
                            <th class="p-3">Netmask</th>
                            <th class="p-3">MTU</th>
                            <th class="p-3">TX (KB/s)</th>
                            <th class="p-3">RX (KB/s)</th>
                            <th class="p-3 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="interface-table"></tbody>
                </table>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
            <!-- Parameter Settings Configuration -->
            <div class="bg-gray-800 rounded-lg p-6 shadow-md">
                <h2 class="text-xl font-semibold mb-4 text-indigo-300">Modify Interface Parameters</h2>
                <form id="config-form" class="space-y-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Select Interface</label>
                        <select id="config-iface" class="w-full bg-gray-700 border border-gray-600 rounded p-2 text-white"></select>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">New IP Address</label>
                        <input type="text" id="config-ip" placeholder="e.g. 192.168.1.100" class="w-full bg-gray-700 border border-gray-600 rounded p-2 text-white">
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Subnet Mask</label>
                        <input type="text" id="config-netmask" placeholder="e.g. 24 or 255.255.255.0" class="w-full bg-gray-700 border border-gray-600 rounded p-2 text-white">
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">MTU</label>
                        <input type="number" id="config-mtu" placeholder="e.g. 1500" class="w-full bg-gray-700 border border-gray-600 rounded p-2 text-white">
                    </div>
                    <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-medium p-2 rounded transition">Apply Changes</button>
                </form>
            </div>

            <!-- Bridge Management -->
            <div class="bg-gray-800 rounded-lg p-6 shadow-md">
                <h2 class="text-xl font-semibold mb-4 text-emerald-400">Bridge Management</h2>
                <div class="mb-6">
                    <h3 class="text-sm font-medium text-gray-400 mb-2">Create Bridge</h3>
                    <form id="bridge-form" class="space-y-4">
                        <input type="text" id="bridge-name" placeholder="Bridge Name (e.g., br0)" class="w-full bg-gray-700 border border-gray-600 rounded p-2 text-white" required>
                        <div>
                            <label class="block text-sm text-gray-400 mb-2">Select Member Interfaces:</label>
                            <div id="bridge-checkbox-container" class="bg-gray-700 p-3 rounded border border-gray-600 max-h-40 overflow-y-auto space-y-2"></div>
                        </div>
                        <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-medium p-2 rounded transition">Create Bridge</button>
                    </form>
                </div>
                <hr class="border-gray-700 my-4">
                <div>
                    <h3 class="text-sm font-medium text-gray-400 mb-2">Teardown Existing Bridge</h3>
                    <div class="flex gap-2">
                        <select id="del-bridge-dropdown" class="w-full bg-gray-700 border border-gray-600 rounded p-2 text-white">
                            <option value="">-- Select active bridge interface --</option>
                        </select>
                        <button id="btn-delete-bridge" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded transition">Delete</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- WAN Traffic Emulation (tc-netem) -->
        <!-- Changed border-orange-900/40 to border-red-900/40 -->
        <div class="bg-gray-800 rounded-lg p-6 shadow-md border border-red-900/40">
            <div class="flex justify-between items-center mb-2">
                <div class="flex items-center space-x-2">
                    <!-- CHANGE: Changed text-orange-400 to text-red-400 -->
                    <h2 class="text-xl font-semibold text-red-400">WAN Network Emulation (tc-netem)</h2>
                </div>
                <!-- Changed text-orange-300 to text-red-400 -->
                <button type="button" onclick="readCurrentNetem()" class="text-xs bg-gray-700 hover:bg-gray-600 text-red-400 px-3 py-1 rounded border border-gray-600 transition">🔄 Read Active Configuration</button>
            </div>
            <p class="text-xs text-gray-400 mb-6">Simulate degraded network links by injecting artificial lag, drops, or corruption on outbound traffic.</p>
            
            <form id="netem-form" class="space-y-6">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Target Interface</label>
                        <select id="netem-iface" onchange="readCurrentNetem()" class="w-full bg-gray-700 border border-gray-600 rounded p-2 text-white"></select>
                    </div>
                    <div></div>

                    <!-- Sliders -->
                    <div>
                        <div class="flex justify-between text-sm mb-1">
                            <span class="text-gray-300">Latency / Delay</span>
                            <!-- CHANGE: Changed text-orange-300 to text-red-400 -->
                            <span class="text-red-400 font-mono" id="val-delay">0 ms</span>
                        </div>
                        <!-- CHANGE: Changed accent-orange-500 to accent-red-500 -->
                        <input type="range" id="netem-delay" min="0" max="1000" value="0" class="w-full accent-red-500 bg-gray-700 h-2 rounded-lg cursor-pointer">
                    </div>
                    <div>
                        <div class="flex justify-between text-sm mb-1">
                            <span class="text-gray-300">Jitter</span>
                            <span class="text-red-400 font-mono" id="val-jitter">0 ms</span>
                        </div>
                        <input type="range" id="netem-jitter" min="0" max="200" value="0" class="w-full accent-red-500 bg-gray-700 h-2 rounded-lg cursor-pointer">
                    </div>
                    <div>
                        <div class="flex justify-between text-sm mb-1">
                            <span class="text-gray-300">Packet Loss</span>
                            <span class="text-red-400 font-mono" id="val-loss">0 %</span>
                        </div>
                        <input type="range" id="netem-loss" min="0" max="100" step="0.5" value="0" class="w-full accent-red-500 bg-gray-700 h-2 rounded-lg cursor-pointer">
                    </div>
                    <div>
                        <div class="flex justify-between text-sm mb-1">
                            <span class="text-gray-300">Packet Corruption</span>
                            <span class="text-red-400 font-mono" id="val-corrupt">0 %</span>
                        </div>
                        <input type="range" id="netem-corrupt" min="0" max="100" step="0.5" value="0" class="w-full accent-red-500 bg-gray-700 h-2 rounded-lg cursor-pointer">
                    </div>
                    <div>
                        <div class="flex justify-between text-sm mb-1">
                            <span class="text-gray-300">Packet Duplication</span>
                            <span class="text-red-400 font-mono" id="val-duplicate">0 %</span>
                        </div>
                        <input type="range" id="netem-duplicate" min="0" max="100" step="0.5" value="0" class="w-full accent-red-500 bg-gray-700 h-2 rounded-lg cursor-pointer">
                    </div>
                    <div>
                        <div class="flex justify-between text-sm mb-1">
                            <span class="text-gray-300">Packet Reordering</span>
                            <span class="text-red-400 font-mono" id="val-reorder">0 %</span>
                        </div>
                        <input type="range" id="netem-reorder" min="0" max="100" step="0.5" value="0" class="w-full accent-red-500 bg-gray-700 h-2 rounded-lg cursor-pointer">
                    </div>
                </div>

                <div class="flex gap-4 pt-2">
                    <!-- FIX HERE: Changed bg-orange-600 hover:bg-orange-700 to bg-red-600 hover:bg-red-700 -->
                    <button type="submit" class="flex-1 bg-green-600 hover:bg-green-700 text-white font-medium p-2.5 rounded transition shadow-md">Apply Simulation Rules</button>
                    <button type="button" id="btn-clear-netem" class="bg-gray-700 hover:bg-gray-600 text-gray-200 font-medium px-6 py-2.5 rounded transition">Reset / Clear Rules</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        const sliders = ['delay', 'jitter', 'loss', 'corrupt', 'duplicate', 'reorder'];
        
        sliders.forEach(id => {
            const el = document.getElementById(`netem-${id}`);
            const output = document.getElementById(`val-${id}`);
            const unit = (id === 'delay' || id === 'jitter') ? ' ms' : ' %';
            el.addEventListener('input', () => { output.textContent = el.value + unit; });
        });

        async function readCurrentNetem() {
            const iface = document.getElementById('netem-iface').value;
            if(!iface) return;
            try {
                const res = await fetch(`/api/netem/status?interface=${iface}`);
                const data = await res.json();
                if(data.status === 'success') {
                    sliders.forEach(id => {
                        const val = data.config[id];
                        const el = document.getElementById(`netem-${id}`);
                        const output = document.getElementById(`val-${id}`);
                        const unit = (id === 'delay' || id === 'jitter') ? ' ms' : ' %';
                        el.value = val;
                        output.textContent = val + unit;
                    });
                }
            } catch (e) { console.error(e); }
        }

        async function triggerInlineBridgeDelete(bridgeName) {
            if(!confirm(`Are you sure you want to completely destroy bridge loop interface ${bridgeName}?`)) return;
            await executeBridgeDeletion(bridgeName);
        }

        async function executeBridgeDeletion(bridgeName) {
            try {
                const res = await fetch('/api/bridge/delete', { 
                    method: 'POST', 
                    headers: {'Content-Type': 'application/json'}, 
                    body: JSON.stringify({ bridge_name: bridgeName }) 
                });
                const data = await res.json();
                alert(data.status === 'success' ? `Bridge ${bridgeName} successfully deleted.` : 'Error: ' + data.output);
                loadInterfaces();
            } catch (e) {
                alert('Failed to connect to backend interface service.');
            }
        }

        async function loadInterfaces() {
            const res = await fetch('/api/interfaces');
            const interfaces = await res.json();
            
            const tableBody = document.getElementById('interface-table');
            const selectDropdown = document.getElementById('config-iface');
            const netemDropdown = document.getElementById('netem-iface');
            const deleteBridgeDropdown = document.getElementById('del-bridge-dropdown');
            const checkboxContainer = document.getElementById('bridge-checkbox-container');
            
            const oldConfigVal = selectDropdown.value;
            const oldNetemVal = netemDropdown.value;
            const oldDeleteVal = deleteBridgeDropdown.value;

            tableBody.innerHTML = ''; 
            selectDropdown.innerHTML = ''; 
            netemDropdown.innerHTML = ''; 
            checkboxContainer.innerHTML = '';
            deleteBridgeDropdown.innerHTML = '<option value="">-- Select active bridge interface --</option>';

            if(interfaces.length === 0) checkboxContainer.innerHTML = '<span class="text-gray-400 text-xs">No local interfaces detected.</span>';

            interfaces.forEach(iface => {
                // Determine if this interface functions as a software bridge linkage
                const isBridge = iface.name.startsWith('br') || iface.name.includes('bridge') || iface.name.includes('br-');

                // Generate targeted direct action layout options
                let actionButtonsHTML = `<button onclick="quickSelect('${iface.name}', '${iface.ip}', '${iface.netmask}', '${iface.mtu}')" class="text-xs text-indigo-400 hover:underline mr-3">Configure</button>`;
                if (isBridge) {
                    actionButtonsHTML += `<button onclick="triggerInlineBridgeDelete('${iface.name}')" class="text-xs text-red-400 hover:underline font-semibold">Delete Bridge</button>`;
                    
                    // Add to the dedicated dropdown control selection menu
                    const bridgeOpt = document.createElement('option');
                    bridgeOpt.value = iface.name;
                    bridgeOpt.textContent = iface.name;
                    deleteBridgeDropdown.appendChild(bridgeOpt);
                }

                const row = document.createElement('tr');
                row.className = "border-b border-gray-800 hover:bg-gray-750";
                row.innerHTML = `
                    <td class="p-3 font-mono ${isBridge ? 'text-emerald-400 font-bold' : 'text-yellow-400'}">${iface.name}</td>
                    <td class="p-3">
                        <span class="px-2 py-1 text-xs font-semibold rounded ${iface.is_up ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}">
                            ${iface.is_up ? 'UP' : 'DOWN'}
                        </span>
                    </td>
                    <td class="p-3 font-mono">${iface.ip}</td>
                    <td class="p-3 font-mono text-gray-400 text-xs">${iface.netmask}</td>
                    <td class="p-3">${iface.mtu}</td>
                    <td class="p-3 font-mono text-blue-400" id="tx-${iface.name}">0.00</td>
                    <td class="p-3 font-mono text-purple-400" id="rx-${iface.name}">0.00</td>
                    <td class="p-3 text-right">${actionButtonsHTML}</td>
                `;
                tableBody.appendChild(row);

                [selectDropdown, netemDropdown].forEach(dropdown => {
                    const opt = document.createElement('option');
                    opt.value = iface.name; opt.textContent = iface.name;
                    dropdown.appendChild(opt);
                });

                // EXCLUSION: Only add to the member selection checkboxes if it is NOT a bridge interface containing "br-"
                if (!iface.name.includes('br-')) {
                    const wrapper = document.createElement('label');
                    wrapper.className = "flex items-center space-x-3 text-sm cursor-pointer hover:bg-gray-600 p-1 rounded";
                    wrapper.innerHTML = `
                        <input type="checkbox" name="bridge_ifaces" value="${iface.name}" class="rounded bg-gray-800 border-gray-600 text-emerald-500 focus:ring-0">
                        <span class="font-mono">${iface.name} (${iface.ip})</span>
                    `;
                    checkboxContainer.appendChild(wrapper);
                }
            });

            if (oldConfigVal) selectDropdown.value = oldConfigVal;
            if (oldDeleteVal && deleteBridgeDropdown.querySelector(`option[value="${oldDeleteVal}"]`)) deleteBridgeDropdown.value = oldDeleteVal;
            if (oldNetemVal) { netemDropdown.value = oldNetemVal; } else { readCurrentNetem(); }
        // Restore previous parameter selection configurations if they exist
            if (oldConfigVal) selectDropdown.value = oldConfigVal;
            if (oldDeleteVal && deleteBridgeDropdown.querySelector(`option[value="${oldDeleteVal}"]`)) {
                deleteBridgeDropdown.value = oldDeleteVal;
            }

            // OPTIMIZATION: Check if an active bridge interface exists to default Netem selection
            const activeBridges = interfaces.filter(iface => iface.name.includes('br-'));
            
            if (activeBridges.length > 0) {
                // Default to the first available bridge interface found (e.g., br-lan0)
                netemDropdown.value = activeBridges[0].name;
            } else if (oldNetemVal && netemDropdown.querySelector(`option[value="${oldNetemVal}"]`)) {
                // Fallback to the previously selected interface if no bridge exists
                netemDropdown.value = oldNetemVal;
            } else {
                // Fallback to the first item in the dropdown list
                if (netemDropdown.options.length > 0) {
                    netemDropdown.selectedIndex = 0;
                }
            }

            // Instantly pull active tc-netem delay/loss stats for the now-selected interface
            readCurrentNetem();
        }

        async function updateUtilization() {
            try {
                const res = await fetch('/api/utilization');
                const data = await res.json();
                for (const [iface, stats] of Object.entries(data)) {
                    const txEl = document.getElementById(`tx-${iface}`);
                    const rxEl = document.getElementById(`rx-${iface}`);
                    if (txEl) txEl.textContent = stats.tx_kbps;
                    if (rxEl) rxEl.textContent = stats.rx_kbps;
                }
            } catch (e) {}
        }

        function quickSelect(name, ip, netmask, mtu) {
            document.getElementById('config-iface').value = name;
            document.getElementById('netem-iface').value = name;
            document.getElementById('config-ip').value = ip !== 'N/A' ? ip : '';
            document.getElementById('config-netmask').value = netmask !== 'N/A' ? netmask : '24';
            document.getElementById('config-mtu').value = mtu;
            readCurrentNetem();
        }

        document.getElementById('config-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const payload = {
                interface: document.getElementById('config-iface').value,
                ip: document.getElementById('config-ip').value,
                netmask: document.getElementById('config-netmask').value,
                mtu: document.getElementById('config-mtu').value
            };
            const res = await fetch('/api/configure', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            const data = await res.json();
            alert(data.status === 'success' ? 'Parameters updated successfully!' : 'Error: ' + data.output);
            loadInterfaces();
        });

        document.getElementById('bridge-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            let bridgeNameInput = document.getElementById('bridge-name').value.trim();
            
            // Automatically prepend 'br-' if the user didn't type it
            if (!bridgeNameInput.startsWith('br-')) {
                bridgeNameInput = 'br-' + bridgeNameInput;
            }

            const checkedBoxes = document.querySelectorAll('input[name="bridge_ifaces"]:checked');
            const payload = {
                bridge_name: bridgeNameInput,
                interfaces: Array.from(checkedBoxes).map(cb => cb.value)
            };
            
            const res = await fetch('/api/bridge/create', { 
                method: 'POST', 
                headers: {'Content-Type': 'application/json'}, 
                body: JSON.stringify(payload) 
            });
            const data = await res.json();
            
            alert(data.status === 'success' ? `Bridge "${bridgeNameInput}" created successfully!` : 'Error: ' + data.output);
            
            // Clear input and refresh UI fields
            document.getElementById('bridge-name').value = '';
            loadInterfaces();
        });

        document.getElementById('btn-delete-bridge').addEventListener('click', async () => {
            const bridgeName = document.getElementById('del-bridge-dropdown').value;
            if(!bridgeName) return alert('Please select a valid bridge interface from the dropdown menu to proceed.');
            if(!confirm(`Are you sure you want to completely destroy bridge loop interface ${bridgeName}?`)) return;
            await executeBridgeDeletion(bridgeName);
        });

        document.getElementById('netem-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const payload = {
                interface: document.getElementById('netem-iface').value,
                delay: document.getElementById('netem-delay').value,
                jitter: document.getElementById('netem-jitter').value,
                loss: document.getElementById('netem-loss').value,
                corrupt: document.getElementById('netem-corrupt').value,
                duplicate: document.getElementById('netem-duplicate').value,
                reorder: document.getElementById('netem-reorder').value
            };
            const res = await fetch('/api/netem/apply', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            const data = await res.json();
            alert(data.status === 'success' ? 'Simulation active.' : 'Error: ' + data.output);
        });

        document.getElementById('btn-clear-netem').addEventListener('click', async () => {
            const iface = document.getElementById('netem-iface').value;
            await fetch('/api/netem/clear', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ interface: iface }) });
            sliders.forEach(id => {
                document.getElementById(`netem-${id}`).value = 0;
                document.getElementById(`val-${id}`).textContent = (id === 'delay' || id === 'jitter') ? '0 ms' : '0 %';
            });
            alert('Simulation rules wiped clean.');
        });

        loadInterfaces();
        setInterval(updateUtilization, 2000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)