//%attributes = {"invisible":true}
#DECLARE($state : Object)

ASSERT($state.running=False)
ASSERT($state.available=False)
ASSERT($state.lock="unknown")
ASSERT(Value type($state.observedAt)=Is null)
