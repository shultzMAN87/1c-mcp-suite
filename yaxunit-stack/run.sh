export DISPLAY=:0
rm -f /tmp/designer.log
/opt/1cv8/x86_64/current/1cv8 DESIGNER /S onec-server/test_demo /LoadConfigFromFiles /tmp/sandbox/demo-config /UpdateDBCfg /DisableStartupMessages /DisableStartupDialogs /Out /tmp/designer.log
echo "REAL_EXIT=$?"
echo "=== /tmp/designer.log ==="
cat /tmp/designer.log
