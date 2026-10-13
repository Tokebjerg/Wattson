# Wattson 0.32.2: recover zero Easee charger limit

When the active EV plan requests charging but Easee is waiting and reports its charger-level dynamic current cap below the requested offer, Wattson reasserts the plan's current offer and resume command. The charger-level cap is bounded by the highest phase current the plan permits; the independently configured Easee circuit cap remains the physical safety limit.

Cap repairs are rate-limited to one every three minutes and at most three repairs per unchanged EV intent. Repairs do not run for pause plans or when the observed charger cap already meets the offer. Existing start supervision and fault alerts remain in place if Easee does not converge.

The regression simulation injects a zero charger cap and verifies charging resumes at the intended offer with zero modeled grid import or battery support. It also verifies a 16 A request is clamped to a 10 A circuit offer.
