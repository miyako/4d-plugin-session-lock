//%attributes = {"invisible":true}
#DECLARE($value : Variant)

ASSERT(Value type($value)=Is text)
ASSERT(Match regex("^(0|[1-9][0-9]*)$"; $value))
ASSERT(Length($value)<=20)
If (Length($value)=20)
    ASSERT($value<="18446744073709551615")
End if
