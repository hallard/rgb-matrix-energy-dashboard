# RGB Matrix Energy Dashboard

A 128x64 RGB LED matrix dashboard for home energy monitoring, inspired by
[OpenEnergyMonitor](https://openenergymonitor.org/). It reads live power
values (PV production, grid import, grid export) from MQTT and renders:

- **Top row**: instant **USE / EXP / PV** in watts
- **Middle**: stacked 24h-ish history chart (yellow = PV, blue = grid import)
- **Bottom**: cycling page between daily share ratios (Grid / Export / Direct %)
  and daily energy totals (Conso / Export / Prod PV kWh)

Built for the Raspberry Pi Zero 2W with the
[Adafruit RGB Matrix HAT (PWM)](https://www.adafruit.com/product/2345).
The panel I used was sold as a single 128x64 unit — internally it may well
be two 64x64 tiles chained, but it was already wired and presented as one
128x64 module. Yours may have a different physical layout, in which case the
`display.hardware_mapping`, `cols`, `rows`, `chain_length` and `parallel`
values will need to be adjusted.

**Strongly recommended before running this project**: get the
[hzeller rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)
sample programs working first (e.g. `demo`, `runtext`, `image-viewer`). Once
those render correctly on your hardware you will know the exact options to
plug into the YAML config here.

Optionally adapts the matrix brightness from ambient light using a
**Pimoroni LTR-559** light sensor on the I2C bus.

## Hardware

- Raspberry Pi (tested on Pi Zero 2W)
- Adafruit RGB Matrix HAT (PWM hack soldered for flicker-free output)
- Two 64x64 RGB LED panels chained for 128x64 total
- (Optional) Pimoroni LTR-559 light sensor on I2C bus 1, address `0x23`
- 5V / 4A+ power supply for the panels

## Software prerequisites

### 1. Build and install the RGB matrix library

This project drives the panels through
[hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix).
You need to build that library **and** install its Python bindings into the
same Python environment you will run the dashboard from. Build deps first:

```bash
sudo apt install -y python3-dev python3-pip python3-venv build-essential \
                    libgraphicsmagick++-dev libwebp-dev cython3
```

### 2. Create a Python virtualenv

The dashboard's shebang is hardcoded to `/home/pi/rgbenv` for convenience:

```bash
#!/home/pi/rgbenv/bin/python3
```

If you want a different venv path, **edit the first line of
`energy_dashboard.py` accordingly**, or run the script through your venv's
Python explicitly. Then create the venv (use `--system-site-packages` so that
optional system packages like `smbus2` are visible):

```bash
python3 -m venv ~/rgbenv --system-site-packages
~/rgbenv/bin/pip install --upgrade pip wheel
```

### 3. Compile the Python bindings into the venv

```bash
git clone https://github.com/hzeller/rpi-rgb-led-matrix.git
cd rpi-rgb-led-matrix
make build-python PYTHON=$HOME/rgbenv/bin/python3
sudo make install-python PYTHON=$HOME/rgbenv/bin/python3
```

Sanity check:

```bash
~/rgbenv/bin/python3 -c "from rgbmatrix import RGBMatrix; print('ok')"
```

### 4. Install the dashboard's Python dependencies

```bash
~/rgbenv/bin/pip install paho-mqtt pyyaml Pillow ltr559
```

(`ltr559` is only needed if you use the Pimoroni ambient brightness sensor.)

### 5. Enable I2C (only for the LTR-559 light sensor)

Uncomment `dtparam=i2c_arm=on` in `/boot/firmware/config.txt` (or
`/boot/config.txt` on older releases), then reboot. `sudo apt install
i2c-tools` lets you check the bus with `i2cdetect -y 1` — the LTR-559 should
appear at address `0x23`.

### 6. An MQTT broker

You need a broker publishing the topics listed below.

## MQTT topics expected

- `pv`           : instant PV production in W (raw number)
- `import_grid`  : instant grid import in W
- `export_grid`  : instant grid export in W (injection)
- `linky_meter`  : JSON payload with daily counters
  (`TDAY` = kWh consumed from grid, `PTDAY` = kWh exported)
- `solar_meter`  : daily PV production total (raw number or JSON field)

All topic names are configurable; see `config_sample.yaml`.

## Feeding the topics from Home Assistant

If your inverter, PV optimizers, Linky teleinfo or smart meter are already
integrated in Home Assistant, you typically don't need any new hardware
plumbing — just publish the existing HA sensors to MQTT with a tiny
automation per topic. The dashboard then consumes those topics as if they
came from any other source.

A few conventions help:

- Use `retain: true` so the dashboard sees the last value as soon as it
  connects (no waiting for the next state change).
- Trigger on `state` of the sensors you want to publish — HA will fire the
  automation each time the value updates.
- Add an availability template guard when summing multiple sensors, so a
  transient `unavailable` from one source doesn't poison the published value.
- Make sure units match what the dashboard expects (see `linky_unit` and
  `solar_unit` in `config_sample.yaml`). When you mix sources with different
  units, normalize inside the template (e.g. divide Wh by 1000 to send kWh).

### Example : sum several sources with unit normalization

This adds APsystems ECU production (kWh) and EcoFlow PowerStream production
(Wh, so divided by 1000), guards against `unavailable`/`unknown` states, and
publishes the total in kWh:

```yaml
alias: Publish Solar Production Day
description: Publish total production of ECU + PowerStream on MQTT
triggers:
  - trigger: state
    entity_id:
      - sensor.ecu_today_energy
      - sensor.ps_production_day
conditions:
  - condition: template
    value_template: >
      {{ states('sensor.ecu_today_energy') not in ['unavailable',
      'unknown', 'none', None]
         and states('sensor.ps_production_day') not in ['unavailable', 'unknown', 'none', None] }}
actions:
  - action: mqtt.publish
    metadata: {}
    data:
      evaluate_payload: false
      retain: true
      topic: energy/solar/production/day
      payload: |
        {{ (
          states('sensor.ecu_today_energy') | float(0)
          +
          (states('sensor.ps_production_day') | float(0) / 1000)
        ) | round(3) }}
mode: single
```

The same pattern works for `energy/solar/ps/min` (instant PV power), the
grid import/export topics, and so on. For the Linky JSON topic
(`energy/linky/METER`), the Linky teleinfo integration usually already
publishes it directly — no automation needed.

## Setup

1. Clone the repo on your Pi:

   ```bash
   git clone https://github.com/<your-user>/rgb-matrix-energy-dashboard.git
   cd rgb-matrix-energy-dashboard
   ```

2. **Copy the sample config and adapt it** (this is the file the dashboard
   actually reads; it is gitignored so your credentials never get committed):

   ```bash
   cp config_sample.yaml config.yaml
   ${EDITOR:-nano} config.yaml
   ```

   At minimum, set:
   - `mqtt.host` / `mqtt.port` / `mqtt.username` / `mqtt.password`
   - `mqtt.topics.*` to match your broker layout
   - `display.fonts.*` to point to PIL bitmap fonts you have locally
     (or use TTF fonts; see comments in the file)

3. (Optional) Plug in the LTR-559 sensor, then verify it answers on the bus:

   ```bash
   sudo apt install i2c-tools
   i2cdetect -y 1     # expect "23" in the grid
   ```

## Running

Manually:

```bash
sudo /home/pi/rgbenv/bin/python3 ./energy_dashboard.py
```

Or pass a custom config path:

```bash
sudo /home/pi/rgbenv/bin/python3 ./energy_dashboard.py /etc/mydash.yaml
```

`sudo` is required because the matrix library needs direct GPIO access.

## Run on boot via systemd

A reference unit file is provided at `energy-dashboard.service`. Install it
and enable autostart:

```bash
sudo cp energy-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now energy-dashboard.service
```

Then watch the logs:

```bash
sudo journalctl -u energy-dashboard -f
```

## Brightness control

If the LTR-559 is connected and `brightness.enabled: true`, the matrix
brightness ramps between `min` and `max` as the measured lux moves between
`lux_min` and `lux_max`, with exponential smoothing (`ema_alpha`).

Because the sensor sits behind the panel, readings are heavily attenuated.
Run the dashboard for a day, watch the debug overlay (lux + brightness %
in the chart area), and tune `lux_min` / `lux_max` accordingly.

## SD card wear

The history database lives **in RAM** (`:memory:` SQLite) at runtime, and is
dumped to the disk file (`history.db`) once per hour and on a clean shutdown
(SIGTERM). Adjust `history.backup_minutes` to trade SD wear for data loss
resilience on a power cut.

## Layout

```
+--------------------------------------------------------------+
|  USEw          EXPw                              PVw         |
|--------------------------------------------------------------|
|  3k ··· ······································ ·········    |
|  2k ·····████······························ ··  ········    |
|  1k ·····████··········█·······█··········  ··  ··········  |
|     YYYYYYYYYBBYYYYBBYYYYBBBBBYYYYBBBBYYYY  YY  YYYYBBBB     |
|--------------------------------------------------------------|
|   Reseau         Export           Direct                     |
|   12%             45%              55%                       |
+--------------------------------------------------------------+
```

(Bottom row alternates every 5s with the daily kWh page.)

## License

MIT. See `LICENSE` if present.
