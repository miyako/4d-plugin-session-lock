//%attributes = {"invisible":true}
If (Application info.headless)
    test_lifecycle
    LOG EVENT(Into system standard outputs; "PASS"; Information message)
End if
