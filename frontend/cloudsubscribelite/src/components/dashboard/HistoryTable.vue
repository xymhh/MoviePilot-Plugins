<template>
  <div class="history-table-root">
    <div class="history-toolbar">
      <div class="history-toolbar-head">
        <div class="history-filter-trigger">
          <v-menu v-model="filtersVisible" :close-on-content-click="false" location="bottom start" offset="6">
            <template #activator="{ props: menuProps }">
              <v-badge :content="activeFilterCount" :model-value="activeFilterCount > 0" color="primary">
                <v-btn
                  v-bind="menuProps"
                  icon="mdi-filter-variant"
                  :color="activeFilterCount ? 'primary' : undefined"
                  variant="text"
                  size="small"
                  title="筛选历史记录" />
              </v-badge>
            </template>
            <v-card class="history-filter-menu" elevation="8">
              <v-card-title class="history-filter-menu-title">
                <span>筛选历史记录</span>
                <v-spacer />
                <v-btn variant="text" size="small" :disabled="!activeFilterCount" @click="clearFilters">清除</v-btn>
              </v-card-title>
              <v-divider />
              <v-card-text class="history-filter-options">
                <div v-if="resourceTypeOptions.length" class="history-filter-group">
                  <span class="history-filter-label">资源类型</span>
                  <v-chip-group v-model="selectedResourceTypes" multiple selected-class="text-primary">
                    <v-chip
                      v-for="option in resourceTypeOptions"
                      :key="option.value"
                      :value="option.value"
                      size="small"
                      variant="tonal"
                      filter>
                      {{ option.title }}
                    </v-chip>
                  </v-chip-group>
                </div>
                <div v-if="sourceOptions.length" class="history-filter-group">
                  <span class="history-filter-label">来源</span>
                  <v-chip-group v-model="selectedSources" multiple selected-class="text-primary">
                    <v-chip
                      v-for="option in sourceOptions"
                      :key="option.value"
                      :value="option.value"
                      size="small"
                      variant="tonal"
                      filter>
                      {{ option.title }}
                    </v-chip>
                  </v-chip-group>
                </div>
                <div class="history-filter-group">
                  <span class="history-filter-label">任务类型</span>
                  <v-chip-group v-model="selectedTaskTypes" multiple selected-class="text-primary">
                    <v-chip
                      v-for="option in taskTypeOptions"
                      :key="option.value"
                      :value="option.value"
                      size="small"
                      variant="tonal"
                      filter>
                      {{ option.title }}
                    </v-chip>
                  </v-chip-group>
                </div>
                <div class="history-filter-group">
                  <span class="history-filter-label">状态</span>
                  <v-chip-group v-model="selectedStatuses" multiple selected-class="text-primary">
                    <v-chip
                      v-for="status in statusOptions"
                      :key="status"
                      :value="status"
                      size="small"
                      variant="tonal"
                      filter>
                      {{ status }}
                    </v-chip>
                  </v-chip-group>
                </div>
              </v-card-text>
            </v-card>
          </v-menu>
        </div>
        <v-text-field
          v-if="!isMobile"
          v-model="keyword"
          class="history-search"
          placeholder="搜索标题或文件名"
          prepend-inner-icon="mdi-magnify"
          clearable
          density="compact"
          variant="outlined"
          hide-details
          @keyup.enter="submitSearch"
          @click:prepend-inner="submitSearch"
          @click:clear="clearSearch" />
        <div v-else class="history-search-trigger">
          <v-menu v-model="searchVisible" :close-on-content-click="false" location="bottom start" offset="6">
            <template #activator="{ props: menuProps }">
              <v-btn
                v-bind="menuProps"
                icon="mdi-magnify"
                :color="keyword ? 'primary' : undefined"
                variant="text"
                size="small"
                title="搜索历史记录" />
            </template>
            <v-card class="history-search-menu" elevation="8">
              <v-card-text class="pa-2">
                <v-text-field
                  v-model="keyword"
                  placeholder="搜索标题或文件名"
                  prepend-inner-icon="mdi-magnify"
                  clearable
                  autofocus
                  density="compact"
                  variant="outlined"
                  hide-details
                  @keyup.enter="submitSearch"
                  @click:prepend-inner="submitSearch"
                  @click:clear="clearSearch" />
              </v-card-text>
            </v-card>
          </v-menu>
        </div>
        <v-spacer />
        <v-btn
          class="delete-selected-button"
          color="error"
          variant="text"
          size="small"
          :disabled="!totalSelectedCount"
          :loading="deletingKey === 'batch'"
          @click.stop="deleteSelected">
          <v-icon icon="mdi-delete-outline" class="history-action-icon" />
          <span class="history-action-label">删除所选{{ totalSelectedCount ? ` (${totalSelectedCount})` : "" }}</span>
        </v-btn>
        <div class="history-actions">
          <v-btn
            color="primary"
            variant="text"
            size="small"
            :loading="reconciling"
            title="核验网盘与本地真实文件，校准待处理记录状态"
            @click.stop="emit('reconcile')">
            <v-icon icon="mdi-check-decagram-outline" class="history-action-icon" />
            <span class="history-action-label">校准状态</span>
          </v-btn>
          <v-btn variant="text" size="small" :loading="loading" title="刷新历史记录" @click.stop="emit('refresh')">
            <v-icon icon="mdi-refresh" class="history-action-icon" />
            <span class="history-action-label">刷新</span>
          </v-btn>
          <v-btn color="error" variant="text" size="small" title="清空历史记录" @click.stop="emit('clear')">
            <v-icon icon="mdi-delete-sweep" class="history-action-icon" />
            <span class="history-action-label">清空历史</span>
          </v-btn>
        </div>
      </div>
      <v-divider />
    </div>

    <div class="history-content">
      <div
        v-if="loading"
        :class="[
          'history-loading-mask',
          {
            'history-loading-mask--empty': !historyGroups.length,
            'history-loading-mask--mobile': isMobile,
            'history-loading-mask--mobile-pagination': isMobile && totalPages > 1,
          },
        ]">
        <div class="history-loading-state">
          <v-progress-circular indeterminate color="primary" size="42" width="4" />
          <span class="text-body-2">正在加载历史记录...</span>
        </div>
      </div>

      <v-data-table-server
        v-if="!isMobile"
        :headers="headers"
        :items="historyGroups"
        :items-length="total"
        :page="page"
        v-model="selectedGroupKeys"
        v-model:expanded="expanded"
        item-value="group_key"
        item-selectable="selectable"
        select-strategy="all"
        show-select
        show-expand
        density="compact"
        hover
        fixed-header
        :items-per-page="pageSize"
        :items-per-page-options="pageSizes"
        items-per-page-text="每页"
        page-text="第 {0}-{1} 条，共 {2} 条"
        no-data-text="暂无符合条件的转存记录"
        loading-text="正在加载转存记录..."
        class="history-table"
        @update:page="changePage"
        @update:items-per-page="changePageSize"
        @click:row="toggleExpanded">
        <template #header.data-table-select>
          <v-checkbox-btn
            :model-value="isPageAllSelected"
            :indeterminate="isPageIndeterminate"
            :disabled="!hasDeletableGroups"
            density="compact"
            color="primary"
            @update:model-value="togglePageSelectAll($event)"
            @click.stop />
        </template>
        <template #item.data-table-select="{ item }">
          <v-checkbox-btn
            :model-value="isGroupAllRecordsSelected(item)"
            :indeterminate="isGroupRecordsIndeterminate(item)"
            :disabled="!item.deletable"
            density="compact"
            color="primary"
            @update:model-value="toggleGroupRecordsSelectAll(item, $event)"
            @click.stop />
        </template>
        <template #item.media="{ item }">
          <div class="media-cell">
            <span class="media-title font-weight-medium" :title="item.title + (item.year ? ` (${item.year})` : '')">
              {{ item.title }}
              <span v-if="item.year">({{ item.year }})</span>
            </span>
            <v-btn
              v-if="mediaDetailLink(item)"
              icon="mdi-open-in-new"
              variant="text"
              size="x-small"
              color="primary"
              title="查看媒体详情"
              class="media-link"
              @click.stop="openMediaDetail(item)" />
          </div>
          <div class="media-meta text-caption text-medium-emphasis">
            {{ item.type }} · {{ item.records.length }} 条记录
            <span>· {{ formatSize(item.total_size) }}</span>
          </div>
        </template>

        <template #item.target_drives="{ item }">
          <div v-if="(item.target_drives || []).length === 1" class="summary-chips">
            <v-chip size="x-small" variant="tonal" :color="driveColor(item.target_drives)">
              {{ item.target_drives[0] }}
            </v-chip>
          </div>
          <div v-else-if="(item.target_drives || []).length > 1" class="mixed-resource-types">
            <v-chip size="x-small" variant="tonal" color="info" prepend-icon="mdi-cloud-outline">
              多网盘 ({{ item.target_drives.length }})
            </v-chip>
          </div>
          <div v-else class="text-caption text-medium-emphasis">--</div>
        </template>

        <template #item.resource_types="{ item }">
          <div v-if="item.resource_types.length === 1" class="summary-chips">
            <v-chip size="x-small" variant="tonal" :color="resourceTypeColor(item.resource_types[0])">
              {{ resourceTypeLabel(item.resource_types[0]) }}
            </v-chip>
          </div>
          <div v-else class="mixed-resource-types">
            <v-chip size="x-small" variant="tonal" color="primary" prepend-icon="mdi-layers-triple-outline">
              混合
            </v-chip>
          </div>
        </template>

        <template #item.sources="{ item }">
          <div class="source-summary">
            <v-chip
              v-if="item.source_items.length > 1"
              size="x-small"
              variant="tonal"
              color="primary"
              prepend-icon="mdi-source-branch">
              多渠道
            </v-chip>
            <v-chip
              v-else-if="item.source_items[0]"
              size="x-small"
              variant="tonal"
              :color="sourceColor(item.source_items[0]?.value)"
              :prepend-icon="sourceIcon(item.source_items[0]?.value)">
              {{ sourceLabel(item.source_items[0]?.value) }}
            </v-chip>
            <span v-else class="text-medium-emphasis">-</span>
          </div>
        </template>

        <template #item.resource_links="{ item }">
          <v-chip
            v-if="item.resource_link_count"
            prepend-icon="mdi-link-variant"
            variant="tonal"
            color="#00acc1"
            size="x-small">
            {{ item.resource_link_count }} 个
            <v-tooltip activator="parent" location="top">
              共 {{ item.resource_link_count }} 个独立网盘分享链接（涵盖 {{ item.records.length }} 集）
            </v-tooltip>
          </v-chip>
          <span v-else class="text-medium-emphasis">-</span>
        </template>

        <template #item.summary="{ item }">
          <div class="summary-counts">
            <span class="text-success">成功 {{ item.success_count }}</span>
            <span class="summary-separator">/</span>
            <span class="text-warning">待完成 {{ item.pending_count }}</span>
            <span class="summary-separator">/</span>
            <span class="text-error">失败 {{ item.failed_count }}</span>
          </div>
        </template>

        <template #item.latest_time="{ item }">
          <span class="latest-time">{{ item.latest_time || "-" }}</span>
        </template>

        <template #item.actions="{ item }">
          <div class="summary-actions">
            <v-btn
              v-if="props.enableCloudUpgrade && canUpgradeGroup(item)"
              icon="mdi-auto-fix"
              color="warning"
              variant="text"
              size="x-small"
              :loading="upgradingKey === historyUpgradeGroupKey(item)"
              :title="item.type === '电影' ? '洗版此电影' : '洗版整个剧集列表'"
              @click.stop="upgradeGroup(item)" />
            <v-btn
              v-if="item.notification_record"
              icon="mdi-bell-ring-outline"
              color="primary"
              variant="text"
              size="x-small"
              :loading="notifyingKey === historyRetryKey(item.notification_record)"
              :title="'补发' + notificationSummaryTitle(item) + '汇总通知'"
              @click.stop="
                emit('notify', {
                  record: item.notification_record,
                  summaryTitle: notificationSummaryTitle(item),
                })
              " />
            <v-btn
              v-if="playItemId(item)"
              icon="mdi-play-circle-outline"
              color="success"
              variant="text"
              size="x-small"
              title="在 Emby 中播放"
              @click.stop="emit('play', playItemId(item))" />
          </div>
        </template>

        <template #expanded-row="{ columns, item }">
          <tr class="detail-row">
            <td :colspan="columns.length" class="pa-0">
              <v-table density="compact" class="detail-table">
                <thead>
                  <tr>
                    <th class="subtable-select-th" style="width: 44px; padding: 0 4px"></th>
                    <th>文件名称</th>
                    <th>目标网盘</th>
                    <th>资源类型</th>
                    <th>视频格式</th>
                    <th>来源渠道</th>
                    <th>资源链接</th>
                    <th>资源大小</th>
                    <th>任务状态</th>
                    <th>完成时间</th>
                    <th class="action-column">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(record, index) in item.records" :key="recordKey(record, index)">
                    <td class="subtable-select-td" style="width: 44px; padding: 0 4px">
                      <v-checkbox-btn
                        :model-value="isRecordSelected(record)"
                        :disabled="!canDeleteRecord(record)"
                        density="compact"
                        color="primary"
                        @update:model-value="toggleRecordSelect(record, item, $event)"
                        @click.stop />
                    </td>
                    <td>
                      <div class="d-flex align-center ga-1" style="min-width: 0; overflow: hidden">
                        <span class="record-name" :title="record.display_name || '-'">
                          {{ record.display_name || "-" }}
                        </span>
                        <v-chip
                          v-if="record.upgrade"
                          size="x-small"
                          color="warning"
                          variant="tonal"
                          class="flex-shrink-0">
                          洗版
                          <v-tooltip activator="parent" location="top">
                            {{ record.upgrade_version_info }}
                          </v-tooltip>
                        </v-chip>
                        <v-chip
                          v-if="record.is_cross_transfer"
                          size="x-small"
                          color="info"
                          variant="tonal"
                          class="flex-shrink-0">
                          跨盘
                          <v-tooltip activator="parent" location="top">
                            {{ record.cross_transfer_title }}
                          </v-tooltip>
                        </v-chip>
                      </div>
                      <div
                        v-if="record.type !== '电影'"
                        class="record-file-name text-caption text-medium-emphasis"
                        :title="record.title || item.title || '-'">
                        {{ record.title || item.title || "-" }}
                      </div>
                    </td>
                    <td>
                      <v-chip
                        size="x-small"
                        variant="tonal"
                        :color="driveColor(record.cloud_drive_name || record.target_drive_name)">
                        {{ record.cloud_drive_name || record.target_drive_name || "-" }}
                      </v-chip>
                    </td>
                    <td>
                      <v-chip size="x-small" variant="tonal" :color="resourceTypeColor(resourceType(record))">
                        {{ resourceTypeLabel(resourceType(record)) }}
                      </v-chip>
                    </td>
                    <td>
                      <v-chip size="x-small" variant="tonal" :color="formatColor(record.file_extension)">
                        {{ record.file_extension || "-" }}
                      </v-chip>
                    </td>
                    <td>
                      <a
                        v-if="record.source_link"
                        :href="record.source_link"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="text-decoration-none"
                        title="打开来源详情页"
                        @click.stop>
                        <v-chip
                          size="x-small"
                          variant="tonal"
                          :color="sourceColor(record.source)"
                          :prepend-icon="sourceIcon(record.source)"
                          class="cursor-pointer">
                          {{ sourceLabel(record.source) }}
                          <v-icon icon="mdi-open-in-new" size="10" class="ml-1" />
                        </v-chip>
                      </a>
                      <v-chip
                        v-else
                        size="x-small"
                        variant="tonal"
                        :color="sourceColor(record.source)"
                        :prepend-icon="sourceIcon(record.source)">
                        {{ sourceLabel(record.source) }}
                      </v-chip>
                    </td>
                    <td>
                      <a
                        v-if="record.resource_link"
                        :href="record.resource_link"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="text-decoration-none"
                        title="打开资源链接"
                        @click.stop>
                        <v-chip
                          size="x-small"
                          variant="tonal"
                          color="#00acc1"
                          prepend-icon="mdi-link-variant"
                          class="cursor-pointer">
                          打开
                          <v-icon icon="mdi-open-in-new" size="10" class="ml-1" />
                        </v-chip>
                      </a>
                      <span v-else class="text-medium-emphasis">-</span>
                    </td>
                    <td class="text-no-wrap">
                      {{ formatSize(record.file_size) }}
                    </td>
                    <td>
                      <v-chip :color="statusColor(record.status)" size="x-small" variant="tonal">
                        {{ record.status }}
                        <v-tooltip v-if="record.failure_reason" activator="parent" location="top" max-width="380">
                          {{ record.failure_reason }}
                        </v-tooltip>
                      </v-chip>
                    </td>
                    <td class="text-no-wrap">{{ record.time || "-" }}</td>
                    <td class="action-column">
                      <div class="action-buttons">
                        <v-btn
                          v-if="props.enableCloudUpgrade && canUpgradeRecord(record)"
                          icon="mdi-auto-fix"
                          color="warning"
                          variant="text"
                          size="x-small"
                          :loading="upgradingKey === historyUpgradeRecordKey(record)"
                          title="洗版此集"
                          @click.stop="upgradeRecord(record)" />
                        <v-btn
                          icon="mdi-reload"
                          color="primary"
                          variant="text"
                          size="x-small"
                          :disabled="!canRetryRecord(record)"
                          :loading="retryingKey === historyRetryKey(record)"
                          :title="retryTitle(record)"
                          @click.stop="emit('retry', record)" />
                        <v-btn
                          icon="mdi-delete-outline"
                          color="error"
                          variant="text"
                          size="x-small"
                          :disabled="!canDeleteRecord(record)"
                          :loading="deletingKey === historyRetryKey(record)"
                          :title="deleteTitle(record)"
                          @click.stop="emit('delete', record)" />
                      </div>
                    </td>
                  </tr>
                </tbody>
              </v-table>
            </td>
          </tr>
        </template>
      </v-data-table-server>

      <div v-else class="history-mobile-list">
        <div class="history-mobile-scroll">
          <div v-if="!loading && !historyGroups.length" class="history-mobile-empty text-body-2 text-medium-emphasis">
            暂无符合条件的转存记录
          </div>
          <v-expansion-panels v-else v-model="expanded" multiple variant="accordion" class="history-mobile-panels">
            <v-expansion-panel
              v-for="item in historyGroups"
              :key="item.group_key"
              :value="item.group_key"
              :class="['history-mobile-panel', { 'history-group-selected': isGroupSelected(item) }]">
              <v-expansion-panel-title class="history-mobile-title">
                <v-checkbox-btn
                  :model-value="isGroupSelected(item)"
                  :disabled="!item.selectable"
                  density="compact"
                  color="primary"
                  :aria-label="`选择 ${item.title}`"
                  @click.stop
                  @update:model-value="selectGroup(item, $event)" />
                <div class="history-mobile-summary">
                  <div class="history-mobile-media-line">
                    <span class="history-mobile-media-title font-weight-medium">
                      {{ item.title }}
                      <span v-if="item.year" class="text-medium-emphasis">({{ item.year }})</span>
                    </span>
                    <div class="history-mobile-summary-actions">
                      <v-btn
                        v-if="props.enableCloudUpgrade && canUpgradeGroup(item)"
                        icon="mdi-auto-fix"
                        color="warning"
                        variant="text"
                        size="x-small"
                        :loading="upgradingKey === historyUpgradeGroupKey(item)"
                        :title="item.type === '电影' ? '洗版此电影' : '洗版整个剧集列表'"
                        @click.stop="upgradeGroup(item)" />
                      <v-btn
                        v-if="item.notification_record"
                        icon="mdi-bell-ring-outline"
                        color="primary"
                        variant="text"
                        size="x-small"
                        :loading="notifyingKey === historyRetryKey(item.notification_record)"
                        :title="'补发' + notificationSummaryTitle(item) + '汇总通知'"
                        @click.stop="
                          emit('notify', {
                            record: item.notification_record,
                            summaryTitle: notificationSummaryTitle(item),
                          })
                        " />
                      <v-btn
                        v-if="playItemId(item)"
                        icon="mdi-play-circle-outline"
                        color="success"
                        variant="text"
                        size="x-small"
                        title="在 Emby 中播放"
                        @click.stop="emit('play', playItemId(item))" />
                      <v-btn
                        v-if="mediaDetailLink(item)"
                        icon="mdi-open-in-new"
                        variant="text"
                        size="x-small"
                        color="primary"
                        title="查看媒体详情"
                        @click.stop="openMediaDetail(item)" />
                    </div>
                  </div>
                  <div class="history-mobile-summary-footer">
                    <div class="history-mobile-tags">
                      <v-chip
                        v-if="item.resource_types.length === 1"
                        :color="resourceTypeColor(item.resource_types[0])"
                        size="x-small"
                        variant="tonal">
                        {{ resourceTypeLabel(item.resource_types[0]) }}
                      </v-chip>
                      <v-chip
                        v-else
                        color="primary"
                        size="x-small"
                        variant="tonal"
                        prepend-icon="mdi-layers-triple-outline">
                        混合
                      </v-chip>
                      <span class="text-caption text-success">成功 {{ item.success_count }} 条</span>
                      <span v-if="item.pending_count" class="text-caption text-warning">
                        待完成 {{ item.pending_count }}
                      </span>
                      <span v-if="item.failed_count" class="text-caption text-error">失败 {{ item.failed_count }}</span>
                    </div>
                    <span class="history-mobile-summary-time text-caption text-medium-emphasis">
                      {{ item.latest_time || "-" }}
                    </span>
                  </div>
                </div>
              </v-expansion-panel-title>
              <v-expansion-panel-text class="history-mobile-details">
                <div
                  v-for="(record, index) in item.records"
                  :key="recordKey(record, index)"
                  class="history-mobile-record">
                  <div class="history-mobile-record-head">
                    <v-checkbox-btn
                      :model-value="isRecordSelected(record)"
                      :disabled="!canDeleteRecord(record)"
                      density="compact"
                      color="primary"
                      class="history-mobile-check"
                      @update:model-value="toggleRecordSelect(record, item, $event)"
                      @click.stop />
                    <div class="history-mobile-record-body">
                      <div class="history-mobile-record-name-line">
                        <span class="record-name" :title="record.display_name || '-'">
                          {{ record.display_name || "-" }}
                        </span>
                        <v-chip v-if="record.upgrade" size="x-small" color="warning" variant="tonal">
                          洗版
                          <v-tooltip activator="parent" location="top">
                            {{ record.upgrade_version_info }}
                          </v-tooltip>
                        </v-chip>
                        <v-chip v-if="record.is_cross_transfer" size="x-small" color="info" variant="tonal">
                          跨盘
                          <v-tooltip activator="parent" location="top">
                            {{ record.cross_transfer_title }}
                          </v-tooltip>
                        </v-chip>
                        <v-chip :color="statusColor(record.status)" size="x-small" variant="tonal">
                          {{ record.status }}
                          <v-tooltip v-if="record.failure_reason" activator="parent" location="top" max-width="380">
                            {{ record.failure_reason }}
                          </v-tooltip>
                        </v-chip>
                      </div>
                      <div
                        v-if="record.type !== '电影'"
                        class="history-mobile-file text-caption text-medium-emphasis"
                        :title="record.title || item.title || '-'">
                        {{ record.title || item.title || "-" }}
                      </div>
                    </div>
                    <v-spacer />
                    <div class="action-buttons history-mobile-actions">
                      <v-btn
                        v-if="props.enableCloudUpgrade && canUpgradeRecord(record)"
                        icon="mdi-auto-fix"
                        color="warning"
                        variant="text"
                        size="x-small"
                        :loading="upgradingKey === historyUpgradeRecordKey(record)"
                        title="洗版此集"
                        @click.stop="upgradeRecord(record)" />
                      <v-btn
                        icon="mdi-reload"
                        color="primary"
                        variant="text"
                        size="x-small"
                        :disabled="!canRetryRecord(record)"
                        :loading="retryingKey === historyRetryKey(record)"
                        :title="retryTitle(record)"
                        @click.stop="emit('retry', record)" />
                      <v-btn
                        icon="mdi-delete-outline"
                        color="error"
                        variant="text"
                        size="x-small"
                        :disabled="!canDeleteRecord(record)"
                        :loading="deletingKey === historyRetryKey(record)"
                        :title="deleteTitle(record)"
                        @click.stop="emit('delete', record)" />
                    </div>
                  </div>
                  <div class="history-mobile-record-footer">
                    <div class="history-mobile-record-meta text-caption text-medium-emphasis">
                      <span>{{ resourceTypeLabel(resourceType(record)) }}</span>
                      <a
                        v-if="record.source_link"
                        :href="record.source_link"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="source-link"
                        title="打开来源详情页"
                        @click.stop>
                        {{ sourceLabel(record.source) }}
                      </a>
                      <span v-else>{{ sourceLabel(record.source) }}</span>
                      <a
                        v-if="record.resource_link"
                        :href="record.resource_link"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="source-link"
                        title="打开资源链接"
                        aria-label="打开资源链接"
                        @click.stop>
                        <v-icon icon="mdi-link-variant" size="x-small" />
                      </a>
                      <span>{{ formatSize(record.file_size) }}</span>
                    </div>
                    <span class="history-mobile-record-time text-caption text-medium-emphasis">
                      {{ record.time || "-" }}
                    </span>
                  </div>
                </div>
              </v-expansion-panel-text>
            </v-expansion-panel>
          </v-expansion-panels>
        </div>
        <v-pagination
          v-if="totalPages > 1"
          :model-value="page"
          :length="totalPages"
          :total-visible="5"
          density="compact"
          class="history-mobile-pagination"
          @update:model-value="changePage" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue"
import { useDisplay } from "vuetify"
import {
  getDriveColor,
  getResourceTypeName,
  getSourceColor,
  getSourceIcon,
  getSourceName,
  getTypeColor,
} from "../../composables/resourceUtils"

const props = defineProps({
  items: { type: Array, default: () => [] },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 10 },
  total: { type: Number, default: 0 },
  totalPages: { type: Number, default: 1 },
  filterOptions: { type: Object, default: () => ({}) },
  embyPlayItems: { type: Object, default: () => ({}) },
  loading: Boolean,
  reconciling: Boolean,
  retryingKey: { type: String, default: "" },
  notifyingKey: { type: String, default: "" },
  upgradingKey: { type: String, default: "" },
  enableCloudUpgrade: Boolean,
})
const emit = defineEmits([
  "reconcile",
  "refresh",
  "clear",
  "delete",
  "delete-groups",
  "selection-change",
  "notify",
  "upgrade",
  "play",
  "open-media",
  "query-change",
])
const display = useDisplay()
const isMobile = computed(() => display.xs.value)
const keyword = ref("")
const appliedKeyword = ref("")
const selectedResourceTypes = ref([])
const selectedSources = ref([])
const selectedTaskTypes = ref([])
const selectedStatuses = ref([])
const expanded = ref([])
const selectedGroupKeys = ref([])
const selectedRecordKeys = ref([])
let groupSelectionFromRecord = false
const filtersVisible = ref(false)
const searchVisible = ref(false)
let lastQuerySignature = ""

const statusOptions = ["处理中", "下载中", "成功", "失败"]
const taskTypeOptions = [
  { title: "跨盘", value: "cross_transfer" },
  { title: "洗版", value: "upgrade" },
]
const pageSizes = [
  { value: 10, title: "10" },
  { value: 20, title: "20" },
  { value: 50, title: "50" },
]
const headers = [
  { title: "媒体名称", key: "media", sortable: false, width: "23%" },
  { title: "目标网盘", key: "target_drives", sortable: false, width: "10%" },
  { title: "资源类型", key: "resource_types", sortable: false, width: "8%" },
  { title: "来源渠道", key: "sources", sortable: false, width: "10%" },
  { title: "资源链接", key: "resource_links", sortable: false, width: "8%" },
  { title: "汇总统计", key: "summary", sortable: false, width: "15%" },
  { title: "完成时间", key: "latest_time", width: 172 },
  { title: "", key: "actions", sortable: false, width: 112 },
  { title: "", key: "data-table-expand", width: 44 },
]

const resourceTypeOptions = computed(() =>
  uniqueOptions(props.filterOptions?.resourceTypes || []).map((value) => ({
    title: resourceTypeLabel(value),
    value,
  })),
)
const sourceOptions = computed(() =>
  uniqueOptions(props.filterOptions?.sources || []).map((value) => ({ title: sourceLabel(value), value })),
)

function notificationSummaryTitle(item) {
  const title = String(item?.title || "未知媒体").trim()
  const year = String(item?.year || "").trim()
  return year ? title + "（" + year + "）" : title
}

const historyGroups = computed(() => (Array.isArray(props.items) ? props.items : []))
const activeFilterCount = computed(
  () =>
    selectedResourceTypes.value.length +
    selectedSources.value.length +
    selectedTaskTypes.value.length +
    selectedStatuses.value.length,
)
const selectedGroups = computed(() => {
  const keys = new Set(selectedGroupKeys.value)
  const recordKeys = new Set(selectedRecordKeys.value)
  return historyGroups.value.filter(
    (group) =>
      keys.has(group.group_key) || (group.records || []).some((record) => recordKeys.has(recordIdentifier(record))),
  )
})
const groupedItemKeys = computed(() => new Set(historyGroups.value.map((group) => group.group_key)))

function historySeasonEpisodes(group) {
  const episodes = {}
  for (const record of group?.records || []) {
    const season = Number(record?.season || 0)
    const episode = Number(record?.episode || 0)
    if (season <= 0 || episode <= 0) continue
    const key = String(season)
    if (!episodes[key]) episodes[key] = []
    episodes[key].push(episode)
  }
  return Object.fromEntries(
    Object.entries(episodes).map(([season, values]) => [season, uniqueOptions(values).sort((a, b) => a - b)]),
  )
}

function emitQueryChange(overrides = {}) {
  const query = {
    page: props.page,
    pageSize: props.pageSize,
    keyword: appliedKeyword.value,
    resourceTypes: [...selectedResourceTypes.value],
    sources: [...selectedSources.value],
    taskTypes: [...selectedTaskTypes.value],
    statuses: [...selectedStatuses.value],
    ...overrides,
  }
  const signature = JSON.stringify(query)
  if (signature === lastQuerySignature) return
  lastQuerySignature = signature
  expanded.value = []
  selectedGroupKeys.value = []
  selectedRecordKeys.value = []
  emit("query-change", query)
}

function changePage(value) {
  emitQueryChange({ page: Math.max(1, Number(value) || 1) })
}

function changePageSize(value) {
  emitQueryChange({
    page: 1,
    pageSize: Math.min(50, Math.max(1, Number(value) || 10)),
  })
}

function submitSearch() {
  const nextKeyword = String(keyword.value || "").trim()
  appliedKeyword.value = nextKeyword
  searchVisible.value = false
  emitQueryChange({ page: 1, keyword: nextKeyword })
}

function clearSearch() {
  keyword.value = ""
  appliedKeyword.value = ""
  emitQueryChange({ page: 1, keyword: "" })
}

watch(
  [selectedResourceTypes, selectedSources, selectedTaskTypes, selectedStatuses],
  () => emitQueryChange({ page: 1 }),
  {
    deep: true,
  },
)

watch(
  () => props.page,
  () => {
    expanded.value = []
    selectedGroupKeys.value = []
  },
)

watch(groupedItemKeys, (keys) => {
  selectedGroupKeys.value = selectedGroupKeys.value.filter((key) => keys.has(key))
})

watch(selectedGroupKeys, (keys, previous) => {
  const derived = groupSelectionFromRecord
  groupSelectionFromRecord = false
  const prev = new Set(previous || [])
  const current = new Set(keys)
  const recordKeys = new Set(selectedRecordKeys.value)
  let changed = false
  for (const group of historyGroups.value) {
    const groupKey = group.group_key
    const wasSelected = prev.has(groupKey)
    const isSelected = current.has(groupKey)
    if (wasSelected === isSelected) continue
    for (const record of group.records || []) {
      if (!canDeleteRecord(record)) continue
      const key = recordIdentifier(record)
      if (!key) continue
      if (isSelected) {
        if (derived) continue // 记录派生的分组选中：不补齐同组其它记录
        recordKeys.add(key)
      } else {
        recordKeys.delete(key)
      }
      changed = true
    }
  }
  if (changed) selectedRecordKeys.value = [...recordKeys]
})

watch(
  selectedGroups,
  (groups) => {
    const recordKeys = new Set(selectedRecordKeys.value)
    const recordCount = historyGroups.value.reduce(
      (count, group) =>
        count + (group.records || []).filter((record) => recordKeys.has(recordIdentifier(record))).length,
      0,
    )
    emit("selection-change", {
      groupCount: groups.length,
      recordCount,
      subscribeIds: uniqueOptions(
        groups
          .flatMap((group) => group.records.map((record) => Number(record.subscribe_id || 0)))
          .filter((value) => value > 0),
      ),
      targets: groups.map((group) => ({
        tmdb_id: Number(group.tmdb_id || 0),
        media_type: group.type || "",
        title: group.title || "",
        year: group.year || "",
        seasons: group.seasons || [],
        season_episodes: historySeasonEpisodes(group),
      })),
    })
  },
  { immediate: true },
)

function uniqueOptions(values) {
  return [...new Set(values.filter(Boolean))]
}

function normalizeSource(value) {
  const normalized =
    String(value || "unknown")
      .trim()
      .toLowerCase() || "unknown"
  return ["manual", "手动添加", "手动资源"].includes(normalized) ? "manual" : normalized
}

function sourceLabel(value) {
  return getSourceName(normalizeSource(value))
}

function sourceColor(value) {
  return getSourceColor(normalizeSource(value))
}

function sourceIcon(value) {
  return getSourceIcon(normalizeSource(value))
}

function formatColor(ext) {
  const e = String(ext || "")
    .trim()
    .toUpperCase()
  if (["ISO", "BDMV"].includes(e)) return "#e64a19"
  if (["TS", "M2TS"].includes(e)) return "#0288d1"
  if (["MP4", "MKV"].includes(e)) return "#546e7a"
  return "#607d8b"
}

function resourceType(item) {
  if (typeof item === "string") return item.trim().toLowerCase()
  const configured = String(item?.resource_type || "")
    .trim()
    .toLowerCase()
  return configured || "unknown"
}

function resourceTypeLabel(value) {
  return getResourceTypeName(resourceType(value))
}

function resourceTypeColor(value) {
  return getTypeColor(resourceType(value))
}

function driveColor(value) {
  const name = Array.isArray(value) ? value[0] : value
  return getDriveColor(name)
}

function mediaDetailLink(item) {
  if (!item?.tmdb_id) return ""
  return [
    `#/media?mediaid=tmdb:${encodeURIComponent(String(item.tmdb_id))}`,
    `title=${encodeURIComponent(item.title || "")}`,
    `year=${encodeURIComponent(item.year || "")}`,
    `type=${encodeURIComponent(item.type || "")}`,
  ].join("&")
}

function openMediaDetail(item) {
  const link = mediaDetailLink(item)
  if (link) emit("open-media", link)
}

function playItemId(item) {
  return String(props.embyPlayItems?.[item?.group_key] || "")
}

function statusColor(status) {
  return status === "成功" ? "success" : ["处理中", "下载中"].includes(status) ? "info" : "error"
}

function formatSize(bytes) {
  let value = Number(bytes || 0)
  if (!value) return "-"
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (value < 1024 || unit === "TB") return `${value.toFixed(value >= 100 ? 0 : 1)} ${unit}`
    value /= 1024
  }
  return "-"
}

function toggleExpanded(event, row) {
  if (event?.target?.closest("a, button, input, .v-selection-control")) return
  const item = row?.item?.raw || row?.item || row
  const key = item?.group_key
  if (!key) return
  expanded.value = expanded.value.includes(key)
    ? expanded.value.filter((value) => value !== key)
    : [...expanded.value, key]
}

function recordKey(record, index) {
  return [record.time, record.file_name, record.season, record.episode, index].join(":")
}

function historyRetryKey(record) {
  return [record.time, record.share_url, record.file_name].join("|")
}

function historyUpgradeGroupKey(group) {
  return `group:${group.group_key}`
}

function historyUpgradeRecordKey(record) {
  return `record:${historyRetryKey(record)}`
}

function canUpgradeRecord(record) {
  return (
    record?.type !== "电影" && record?.status === "成功" && !record?.finalize_key && Number(record?.episode || 0) > 0
  )
}

function canUpgradeGroup(group) {
  return (group?.records || []).some((record) => record?.status === "成功" && !record?.finalize_key)
}

function upgradeRecord(record) {
  if (!canUpgradeRecord(record)) return
  emit("upgrade", {
    scope: "record",
    records: [record],
    key: historyUpgradeRecordKey(record),
  })
}

function upgradeGroup(group) {
  if (!canUpgradeGroup(group)) return
  emit("upgrade", {
    scope: "group",
    media: {
      title: group.title,
      year: group.year,
      tmdb_id: group.tmdb_id,
      media_type: group.type === "电影" ? "movie" : "tv",
      season: group.seasons.length === 1 ? group.seasons[0] : null,
    },
    records: group.records.filter((record) => record?.status === "成功" && !record?.finalize_key),
    key: historyUpgradeGroupKey(group),
  })
}

function recordIdentifier(record) {
  return String(record?.record_id || [record?.time, record?.share_url, record?.file_name].join("|")).trim()
}

function isRecordSelected(record) {
  const key = recordIdentifier(record)
  return Boolean(key) && selectedRecordKeys.value.includes(key)
}

function toggleRecordSelect(record, group, checked) {
  if (!canDeleteRecord(record)) return
  const key = recordIdentifier(record)
  if (!key) return
  const set = new Set(selectedRecordKeys.value)
  if (checked) set.add(key)
  else set.delete(key)
  selectedRecordKeys.value = [...set]
  syncGroupSelection(group, set)
}

function syncGroupSelection(group, recordKeys) {
  // 记录级勾选必须同步分组选择态，否则「搜索所选」永远拿不到数量；内容不变则不写回
  const groupKey = group?.group_key
  if (!groupKey) return
  const current = selectedGroupKeys.value
  const shouldSelect = (group.records || []).some((record) => {
    const key = recordIdentifier(record)
    return key && recordKeys.has(key)
  })
  if (shouldSelect === current.includes(groupKey)) return
  if (shouldSelect) {
    groupSelectionFromRecord = true
  }
  selectedGroupKeys.value = shouldSelect ? [...current, groupKey] : current.filter((key) => key !== groupKey)
}

function isGroupAllRecordsSelected(group) {
  const records = (group?.records || []).filter(canDeleteRecord)
  if (!records.length) return false
  return records.every((r) => isRecordSelected(r))
}

function isGroupRecordsIndeterminate(group) {
  const records = (group?.records || []).filter(canDeleteRecord)
  if (!records.length) return false
  const count = records.filter((r) => isRecordSelected(r)).length
  return count > 0 && count < records.length
}

function toggleGroupRecordsSelectAll(group, checked) {
  const records = (group?.records || []).filter(canDeleteRecord)
  if (!records.length) return
  const set = new Set(selectedRecordKeys.value)
  for (const r of records) {
    const key = recordIdentifier(r)
    if (checked) set.add(key)
    else set.delete(key)
  }
  selectedRecordKeys.value = [...set]
  syncGroupSelection(group, set)
}

const effectiveSelectedRecords = computed(() => {
  // 以记录键为唯一口径：分组勾选会补齐记录键，部分取消也能被准确反映
  const selectedKeySet = new Set(selectedRecordKeys.value)
  const recordsMap = new Map()
  for (const group of historyGroups.value) {
    for (const record of group.records || []) {
      const key = recordIdentifier(record)
      if (key && canDeleteRecord(record) && selectedKeySet.has(key)) {
        recordsMap.set(key, record)
      }
    }
  }
  return [...recordsMap.values()]
})

const totalSelectedCount = computed(() => effectiveSelectedRecords.value.length)

const hasDeletableGroups = computed(() =>
  historyGroups.value.some((group) => group.deletable && (group?.records || []).some(canDeleteRecord)),
)

const isPageAllSelected = computed(() => {
  const groups = historyGroups.value.filter((g) => g.deletable && (g?.records || []).some(canDeleteRecord))
  if (!groups.length) return false
  return groups.every((g) => isGroupAllRecordsSelected(g))
})

const isPageIndeterminate = computed(() => {
  if (isPageAllSelected.value) return false
  return effectiveSelectedRecords.value.length > 0
})

function togglePageSelectAll(checked) {
  const set = new Set(selectedRecordKeys.value)
  const groupKeys = new Set(selectedGroupKeys.value)
  for (const group of historyGroups.value) {
    if (!group.deletable) continue
    const records = (group?.records || []).filter(canDeleteRecord)
    if (!records.length) continue
    if (checked) {
      groupKeys.add(group.group_key)
      for (const r of records) {
        set.add(recordIdentifier(r))
      }
    } else {
      groupKeys.delete(group.group_key)
      for (const r of records) {
        set.delete(recordIdentifier(r))
      }
    }
  }
  selectedGroupKeys.value = [...groupKeys]
  selectedRecordKeys.value = [...set]
}

function isGroupSelected(group) {
  return selectedGroupKeys.value.includes(group.group_key)
}

function selectGroup(group, selected) {
  if (!group.selectable) return
  const keys = new Set(selectedGroupKeys.value)
  if (selected) keys.add(group.group_key)
  else keys.delete(group.group_key)
  selectedGroupKeys.value = [...keys]
}

function deleteSelected() {
  if (!effectiveSelectedRecords.value.length) return
  emit("delete-groups", {
    groupCount: selectedGroups.value.length || 1,
    records: effectiveSelectedRecords.value,
  })
}

function clearFilters() {
  selectedResourceTypes.value = []
  selectedSources.value = []
  selectedTaskTypes.value = []
  selectedStatuses.value = []
}

function canRetryRecord(record) {
  return Boolean(record?.can_retry)
}

function retryTitle(record) {
  return record?.retry_title || "当前记录无需重试"
}

function canDeleteRecord(record) {
  return ["成功", "失败"].includes(record?.status) || Boolean(record?.finalize_key)
}

function deleteTitle(record) {
  if (!canDeleteRecord(record)) return "当前记录不能删除"
  return record?.finalize_key ? "删除此条后处理记录" : "删除此条历史记录"
}
</script>

<style scoped>
.history-table-root {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  background: rgb(var(--v-theme-surface));
}

.history-content {
  position: relative;
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.history-loading-mask {
  position: absolute;
  z-index: 5;
  top: 48px;
  right: 0;
  bottom: 56px;
  left: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(var(--v-theme-surface), 0.76);
  backdrop-filter: blur(1px);
}

.history-loading-mask--empty,
.history-loading-mask--mobile {
  top: 0;
  bottom: 0;
}

.history-loading-mask--mobile-pagination {
  bottom: 48px;
}

.history-loading-state {
  display: flex;
  align-items: center;
  flex-direction: column;
  gap: 12px;
  color: rgb(var(--v-theme-primary));
}

.history-toolbar {
  flex: 0 0 auto;
  background: rgb(var(--v-theme-surface));
}

.history-toolbar-head {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 4px;
  padding: 6px 8px;
}

.history-filter-trigger {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
}

.history-search {
  width: min(260px, 28vw);
  min-width: 180px;
  flex: 0 1 260px;
}

.history-search-trigger {
  display: flex;
  align-items: center;
}

.history-search-menu {
  width: min(340px, calc(100vw - 24px));
}

.history-filter-menu {
  width: min(420px, calc(100vw - 24px));
  max-height: min(520px, calc(100vh - 32px));
  overflow-y: auto;
}

.history-filter-menu-title {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  font-size: 0.9rem;
  font-weight: 600;
}

.history-filter-options {
  padding: 10px 12px !important;
}

.history-filter-group + .history-filter-group {
  margin-top: 8px;
}

.history-filter-label {
  display: block;
  margin-bottom: 2px;
  color: rgba(var(--v-theme-on-surface), 0.78);
  font-size: 0.8rem;
  font-weight: 600;
}

.history-filter-group :deep(.v-chip-group) {
  margin: 0;
}

.history-filter-group :deep(.v-slide-group__content) {
  gap: 4px;
  padding: 0;
}

.history-actions {
  display: flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 2px;
}

.history-actions :deep(.v-btn) {
  white-space: nowrap;
}

.history-action-icon {
  margin-right: 6px;
}

.history-toolbar :deep(.v-field) {
  --v-input-control-height: 36px;
  --v-field-input-padding-top: 4px;
  --v-field-input-padding-bottom: 4px;
}

.history-table > :deep(.v-table__wrapper) {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.history-table {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
  flex-direction: column;
}

.history-table :deep(.v-data-table-footer) {
  justify-content: flex-end;
  min-height: 56px;
  padding: 6px 16px;
  flex: 0 0 auto;
  background: rgb(var(--v-theme-surface));
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.history-table :deep(table) {
  width: 100%;
  min-width: 0;
  table-layout: fixed;
}

.summary-counts {
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  min-width: 0;
  overflow: hidden;
  gap: 5px;
  font-size: 0.75rem;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.summary-actions,
.history-mobile-summary-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
}

.summary-separator {
  color: rgba(var(--v-theme-on-surface), 0.38);
  font-weight: 400;
}

.history-table :deep(thead tr),
.history-table :deep(thead th) {
  height: 34px !important;
}

.history-table :deep(tbody > tr:not(.detail-row)) {
  cursor: pointer;
}

.history-table :deep(thead th) {
  padding-top: 0 !important;
  padding-bottom: 0 !important;
}

.summary-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.source-summary {
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 4px;
  min-width: 0;
  white-space: nowrap;
}

.mixed-resource-types {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.detail-row > td {
  background: rgb(var(--v-theme-surface));
}

.detail-row > td::after {
  background: transparent !important;
  opacity: 0 !important;
}

.detail-table :deep(table) {
  width: 100%;
  min-width: 0;
  table-layout: fixed;
}

.detail-table :deep(.v-table__wrapper) {
  overflow-x: auto !important;
  overflow-y: visible !important;
}

.detail-table :deep(tbody tr:hover > td) {
  background: transparent !important;
}

.detail-table :deep(tbody tr > td::after) {
  background: transparent !important;
  opacity: 0 !important;
}

.detail-table :deep(th) {
  height: 32px !important;
  font-size: 0.75rem;
  color: rgb(var(--v-theme-on-surface-variant));
  white-space: nowrap;
}

.detail-table :deep(th.subtable-select-th),
.detail-table :deep(td.subtable-select-td),
.detail-table :deep(th:first-child),
.detail-table :deep(td:first-child) {
  width: 44px !important;
  min-width: 44px !important;
  max-width: 44px !important;
  padding: 0 4px !important;
  text-align: center;
}

.detail-table :deep(th:nth-child(2)),
.detail-table :deep(td:nth-child(2)) {
  width: 16%;
  min-width: 0;
}

.detail-table :deep(th:nth-child(3)),
.detail-table :deep(td:nth-child(3)) {
  width: 9%;
}

.detail-table :deep(th:nth-child(4)),
.detail-table :deep(td:nth-child(4)) {
  width: 8%;
}

.detail-table :deep(th:nth-child(5)),
.detail-table :deep(td:nth-child(5)) {
  width: 8%;
}

.detail-table :deep(th:nth-child(6)),
.detail-table :deep(td:nth-child(6)) {
  width: 9%;
}

.detail-table :deep(th:nth-child(7)),
.detail-table :deep(td:nth-child(7)) {
  width: 7%;
}

.detail-table :deep(th:nth-child(8)),
.detail-table :deep(td:nth-child(8)) {
  width: 7%;
}

.detail-table :deep(th:nth-child(9)),
.detail-table :deep(td:nth-child(9)) {
  width: 72px;
  min-width: 72px;
}

.detail-table :deep(th:nth-child(10)),
.detail-table :deep(td:nth-child(10)) {
  width: 165px;
  min-width: 165px;
}

.detail-table :deep(td:not(.subtable-select-td):not(:first-child):not(:nth-child(2))) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.action-column {
  width: 88px;
  min-width: 88px;
  text-align: center;
  white-space: nowrap;
}

.action-buttons {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 2px;
  width: 100%;
}

.latest-time {
  display: inline-block;
  min-width: 0;
  line-height: 1.35;
  white-space: nowrap;
}

.record-name {
  min-width: 0;
  overflow: hidden;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.record-file-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.media-cell {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 2px;
}

.media-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.media-meta {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.media-link {
  flex: 0 0 auto;
}

.history-mobile-list {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.history-mobile-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
}

.history-mobile-empty {
  display: flex;
  min-height: 100%;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  text-align: center;
}

.history-mobile-panels {
  border-radius: 0;
}

.history-mobile-panel {
  background: rgb(var(--v-theme-surface));
}

.history-mobile-panel + .history-mobile-panel {
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.history-mobile-title {
  min-height: 0 !important;
  padding: 8px 10px !important;
}

.history-mobile-summary {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  column-gap: 8px;
  min-width: 0;
  width: auto;
  flex: 1 1 0;
  overflow: hidden;
}

.history-mobile-summary-footer {
  display: grid;
  grid-column: 1 / -1;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  min-width: 0;
  width: 100%;
  margin-top: 4px;
  gap: 8px;
}

.history-mobile-summary-time {
  justify-self: end;
  white-space: nowrap;
}

.history-group-selected {
  box-shadow: inset 3px 0 0 rgb(var(--v-theme-primary));
}

.history-mobile-media-line,
.history-mobile-tags,
.history-mobile-record-head {
  display: flex;
  align-items: center; /* 复选框 / 文字块 / 操作按钮 中线对齐 */
  min-width: 0;
  gap: 2px;
}

.history-mobile-record-body {
  flex: 1 1 0;
  min-width: 0;
}

.history-mobile-record-name-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.history-mobile-check {
  flex: 0 0 auto;
  margin-top: 0; /* 居中后不再需要上移补偿 */
}

.history-mobile-record .history-mobile-file {
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-mobile-actions {
  flex: 0 0 auto;
}

.history-mobile-record-meta {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 6px;
}

.history-mobile-media-line {
  padding-right: 4px;
}

.history-mobile-summary-actions {
  flex: 0 0 auto;
  margin-left: auto;
}

.history-mobile-media-title {
  display: block;
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-mobile-record-meta {
  flex-wrap: wrap;
  margin-top: 4px;
}

.history-mobile-tags {
  max-width: 100%;
  flex-wrap: nowrap;
  overflow: hidden;
}

.history-mobile-tags > :deep(.v-chip),
.history-mobile-tags > span {
  flex: 0 0 auto;
  white-space: nowrap;
}

.history-mobile-record-footer {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  min-width: 0;
  gap: 8px;
}

.history-mobile-record-meta {
  flex: 1 1 0;
  overflow: hidden;
}

.history-mobile-record-time {
  flex: 0 0 auto;
  margin-left: auto;
  white-space: nowrap;
}

.history-mobile-record-meta > * + *::before {
  margin-right: 6px;
  color: rgba(var(--v-theme-on-surface), 0.38);
  content: "·";
}

.history-mobile-details :deep(.v-expansion-panel-text__wrapper) {
  padding: 0 !important;
}

.history-mobile-record {
  padding: 7px 10px;
  background: rgba(var(--v-theme-on-surface), 0.025);
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.history-mobile-actions {
  flex: 0 0 auto;
  width: auto;
  justify-content: flex-end;
}

.history-mobile-file {
  min-width: 0;
  margin-top: 2px;
  overflow: hidden;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-mobile-pagination {
  flex: 0 0 auto;
  padding: 8px 4px;
  background: rgb(var(--v-theme-surface));
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

@media (min-width: 601px) {
  .history-table {
    font-size: 0.875rem;
  }

  .history-table :deep(thead th) {
    padding: 0 12px !important;
    font-size: 0.8125rem;
  }

  .history-table :deep(tbody > tr:not(.detail-row)) {
    height: 56px !important;
  }

  .history-table :deep(tbody > tr:not(.detail-row) > td) {
    padding: 10px 12px !important;
    overflow: hidden;
  }

  .detail-table :deep(td) {
    padding: 8px 10px !important;
    font-size: 0.8125rem;
  }
}

@media (max-width: 600px) {
  .history-toolbar-head {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 4px 8px;
  }

  .history-toolbar-head > .v-spacer {
    display: block;
    min-width: 0;
    flex: 1 1 auto;
  }

  .history-search {
    width: auto;
    min-width: 0;
    flex: none;
  }

  .history-actions {
    display: flex;
    flex: 0 0 auto;
    gap: 2px;
    padding: 0;
  }

  .history-actions :deep(.v-btn) {
    min-width: 32px;
    width: 32px;
    height: 32px;
    padding: 0;
  }

  .history-action-icon {
    margin: 0;
  }

  .history-action-label {
    display: none;
  }

  .delete-selected-button {
    min-width: 32px !important;
    width: 32px;
    height: 32px;
    flex: 0 0 32px;
  }

  .history-mobile-title :deep(.v-selection-control) {
    align-self: flex-start;
    flex: 0 0 32px;
    margin-top: -2px;
  }

  .history-toolbar :deep(.v-field) {
    --v-input-control-height: 44px;
    min-height: 44px;
  }

  .history-search :deep(.v-input),
  .history-search :deep(.v-input__control),
  .history-search :deep(.v-field),
  .history-search :deep(.v-field__input) {
    min-height: 32px !important;
    height: 32px !important;
    max-height: 32px !important;
  }

  .history-search :deep(.v-field) {
    padding-inline: 8px;
  }

  .history-search :deep(.v-field__input) {
    padding-top: 0 !important;
    padding-bottom: 0 !important;
  }

  .history-search :deep(.v-input__details) {
    display: none;
  }

  .history-mobile-scroll {
    scrollbar-width: none;
    touch-action: pan-y;
  }

  .history-mobile-scroll::-webkit-scrollbar {
    display: none;
  }

  .history-filter-group,
  .history-filter-group + .history-filter-group {
    display: flex;
    align-items: center;
    min-width: 0;
    margin-top: 4px;
    gap: 6px;
  }

  .history-filter-label {
    flex: 0 0 58px;
    margin: 0;
    color: rgba(var(--v-theme-on-surface), 0.82) !important;
    font-size: 0.8rem;
    font-weight: 700;
    opacity: 1 !important;
  }

  .history-filter-group :deep(.v-chip-group) {
    min-width: 0;
    flex: 1 1 auto;
    overflow-x: auto;
  }

  .history-filter-group :deep(.v-slide-group__content) {
    flex-wrap: nowrap;
    min-width: max-content;
  }
}

.source-link {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  color: rgb(var(--v-theme-primary));
  text-decoration: none;
}

.source-link:hover {
  text-decoration: underline;
}

.media-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: rgb(var(--v-theme-primary));
  text-decoration: none;
}

.media-link:hover {
  text-decoration: none;
}
</style>
