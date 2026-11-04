//%attributes = {"invisible":true}
#DECLARE($result : Object)

ASSERT(Value type($result)=Is object)
ASSERT(OB Keys($result).length=2)
ASSERT(Value type($result.success)=Is Boolean)
assert_state($result.state)
