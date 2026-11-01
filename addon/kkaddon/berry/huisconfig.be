# Huisconfig voor de NSPanel: praat via UDP met de Huisconfig-add-on in plaats van via MQTT/AppDaemon.
#
# Dit script vervangt niets van de driver van nspanel-lovelace-ui (autoexec.be): het scherm, de TFT en het flashen blijven daar.
# Het doet drie dingen:
#   * tekst van de add-on (D|...) doorgeven aan het scherm, zoals het commando CustomSend;
#   * tekst van het scherm (CustomRecv) doorsturen naar de add-on (S|...);
#   * de twee relais en de temperatuur aanbieden: hartslag H|{...} elke 15 s en bij elke wijziging, relais zetten met P|n|0/1.
#
# Instellen: zie "Paneel" in de Huisconfig-add-on (daar komt dit bestand met de juiste adressen uit).
# Terugdraaien: haal de regel `load("huisconfig.be")` uit autoexec.be, of verwijder dit bestand, en herstart.

import json
import string
import global

var HUB_HOST = "@HUB_HOST@"
var HUB_PORT = @HUB_PORT@
var LISTEN_PORT = @PANEL_PORT@
var BEAT = 15000 # ms

class Huisconfig : Driver
  var u, last_beat, booted, sent

  def init()
    self.u = nil # pas openen als de wifi er is: bij het opstarten is het netwerk er nog niet
    self.last_beat = 0
    self.booted = true
    self.sent = 0
  end

  def start_udp()
    var w = tasmota.wifi()
    if w == nil || !w.find('up', false) return end
    var u = udp()
    if u.begin("", LISTEN_PORT)
      self.u = u
    else
      print("HUC: UDP-poort " + str(LISTEN_PORT) + " niet beschikbaar")
    end
  end

  def to_hub(text)
    if self.u == nil return end
    self.u.send(HUB_HOST, HUB_PORT, bytes().fromstring(text))
    self.sent += 1
  end

  def temperature()
    var s = tasmota.read_sensors()
    if s == nil return nil end
    var j = (type(s) == 'string') ? json.load(s) : s
    if j == nil return nil end
    var a = j.find('ANALOG')
    if a == nil return nil end
    return a.find('Temperature1')
  end

  def beat()
    if self.u == nil return end # ook een relais dat bij het opstarten schakelt komt hier
    var p = tasmota.get_power()
    var d = {}
    d['p1'] = (size(p) > 0 && p[0]) ? 1 : 0
    d['p2'] = (size(p) > 1 && p[1]) ? 1 : 0
    var t = self.temperature()
    if t != nil d['t'] = t end
    var w = tasmota.wifi()
    if w != nil && w.find('rssi') != nil && w['rssi'] < 0 d['rssi'] = w['rssi'] end
    if self.booted d['boot'] = true end
    self.booted = false
    self.last_beat = tasmota.millis()
    self.to_hub("H|" + json.dump(d))
  end

  def handle(text)
    var i = string.find(text, "|")
    if i < 1 return end
    var tag = text[0 .. i - 1]
    var body = (i + 1 < size(text)) ? text[i + 1 .. -1] : ""
    if tag == "D"
      global.nextion.send(body)
    elif tag == "P"
      var j = string.find(body, "|")
      if j > 0
        var n = int(body[0 .. j - 1])
        if n == 1 || n == 2
          tasmota.set_power(n - 1, body[j + 1 .. -1] == "1")
        end
      end
    elif tag == "Q"
      self.beat()
    end
  end

  def every_50ms()
    if self.u == nil return end
    var n = 0
    while n < 8
      var p = self.u.read()
      if p == nil break end
      self.handle(p.asstring())
      n += 1
    end
  end

  def every_second()
    if self.u == nil
      self.start_udp()
      return
    end
    if tasmota.millis() - self.last_beat >= BEAT
      self.beat()
    end
  end
end

# Opnieuw laden (bv. na een wijziging) ruimt de vorige koppeling eerst op.
if global.contains('huisconfig') && global.huisconfig != nil
  tasmota.remove_driver(global.huisconfig)
  if global.huisconfig.u != nil global.huisconfig.u.close() end
end

var hc = Huisconfig()
global.huisconfig = hc
tasmota.add_driver(hc)

# Een aanraking of een ander bericht van het scherm gaat naar de add-on.
tasmota.remove_rule("CustomRecv", 'huisconfig')
tasmota.add_rule("CustomRecv", def (value) hc.to_hub("S|" + str(value)) end, 'huisconfig')

# Een relais dat verandert (hier of met de knop op de panel) meteen melden.
tasmota.remove_rule("Power1#State", 'huisconfig')
tasmota.remove_rule("Power2#State", 'huisconfig')
tasmota.add_rule("Power1#State", def (value) hc.beat() end, 'huisconfig')
tasmota.add_rule("Power2#State", def (value) hc.beat() end, 'huisconfig')

print("HUC: Huisconfig-koppeling actief, add-on op " + HUB_HOST + ":" + str(HUB_PORT))
