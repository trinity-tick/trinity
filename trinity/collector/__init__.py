"""
Trinity Collector Daemon — v8.4.0
==================================
将主动采集器改造为独立守护进程，持久化后台运行。

组件：
  - CollectorDaemon：守护进程核心，含健康检查与自动重启
  - CLI：python -m trinity.collector start|stop|status

用法：
    python -m trinity.collector start     # 启动守护进程
    python -m trinity.collector stop      # 停止守护进程
    python -m trinity.collector status    # 查看运行状态
"""

from trinity.collector.daemon import CollectorDaemon

__all__ = ["CollectorDaemon"]
__version__ = "8.4.0"
