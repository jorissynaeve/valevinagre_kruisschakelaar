// Minimaal HUISDEV-voorbeeld (protocol 1.0). Kopieer huisdev.h naast deze sketch.
// huisdev-protocol: 1.0
#include <WiFi.h>
#include <WebServer.h>
#include "huisdev.h"

const char* WIFI_SSID = "ssid";      // invullen
const char* WIFI_PASS = "wachtwoord";  // invullen

WebServer server(80);
HuisDev dev;

bool relayOn = false;
int mode = 0;  // 0 uit, 1 aan, 2 auto
uint32_t tSensor = 0, tExtra = 0;
bool extraAdded = false;

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  // Punten: id stabiel (snake_case), label in het Nederlands.
  dev.point("temp", "Temperatuur", HD_FLOAT).unit("°C").decimals(1).devClass("temperature").stateClass("measurement").stale(15000);
  dev.point("relay", "Relais", HD_BOOL).writable([](double n, const String&) {
    relayOn = n != 0;  // hier de pin schakelen
    dev.setBool("relay", relayOn);
    return HdResult::applied();
  });
  dev.point("mode", "Modus", HD_ENUM).option(0, "Uit").option(1, "Aan").option(2, "Auto").writable([](double n, const String&) {
    mode = (int)n;
    dev.setNum("mode", mode);
    return HdResult::applied();
  });
  dev.point("alarm_low_flow", "Alarm: geen doorstroming", HD_BOOL).group("alarmen");
  dev.point("rssi", "WiFi-signaal", HD_INT).unit("dBm").diag();

  dev.action("boost", "Boost starten", [](const String& body) {
    int minutes = (int)hdJsonNum(body, "minutes", 30);
    Serial.printf("boost %d min\n", minutes);
    return HdResult::queued();  // het apparaat meldt de boost later via een punt
  }).arg("minutes", "Duur", HD_INT).unit("min").range(5, 240, 5);

  dev.action("reset", "Fabrieksinstellingen", [](const String&) { return HdResult::rejected("niet geimplementeerd"); })
      .danger("Alle instellingen gaan verloren. Doorgaan?");

  dev.begin("voorbeeld", "Voorbeeld", "1.0.0", &server);
  server.begin();
}

void loop() {
  server.handleClient();
  dev.loop();

  if (millis() - tSensor > 2000) {
    tSensor = millis();
    dev.setNum("temp", 20.0 + (millis() / 1000 % 10) / 10.0);
    dev.setNum("rssi", WiFi.RSSI());
    dev.setBool("alarm_low_flow", false);
  }
  // Dynamisch: na 60 s komt er een punt bij. De rev verandert en de huisconfig past het blok aan.
  if (!extraAdded && millis() - tExtra > 60000) {
    extraAdded = true;
    dev.point("salt", "Zout", HD_FLOAT).unit("g/L").decimals(1);
    dev.setNum("salt", 3.2);
  }
}
