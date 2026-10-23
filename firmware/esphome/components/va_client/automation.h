#pragma once

#include "esphome/core/automation.h"
#include "va_client.h"

#include <string>

namespace esphome {
namespace va_client {

class OnPhaseTrigger : public Trigger<std::string> {
 public:
  explicit OnPhaseTrigger(VaClient *parent) { parent->add_on_phase_trigger(this); }
};

class OnRepeatedFailureTrigger : public Trigger<> {
 public:
  explicit OnRepeatedFailureTrigger(VaClient *parent) {
    parent->add_on_repeated_failure_trigger(this);
  }
};

// Fires on the add-on's `working`: the model has taken the user's turn and
// runs a tool. yaml arms the "stop" wake word on it.
class OnWorkingTrigger : public Trigger<> {
 public:
  explicit OnWorkingTrigger(VaClient *parent) { parent->add_on_working_trigger(this); }
};

}  // namespace va_client
}  // namespace esphome
