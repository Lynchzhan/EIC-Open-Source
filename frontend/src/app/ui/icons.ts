import { defineComponent, h, type Component } from 'vue'
import {
  GitNetworkOutline, ListOutline, NotificationsOutline, BookOutline, CheckmarkCircleOutline,
  CloseCircleOutline, CloseOutline, CodeSlashOutline, ServerOutline, TrashOutline,
  DownloadOutline, CreateOutline, EllipsisHorizontalOutline, FlaskOutline, EyeOutline,
  DocumentOutline, DocumentTextOutline, FolderOpenOutline, FolderOutline, TimeOutline,
  HomeOutline, EnterOutline, KeyOutline, MailOutline, ChatbubblesOutline, AttachOutline,
  PauseCircleOutline, PlayCircleOutline, AddOutline, ReloadOutline, ReturnUpBackOutline,
  ShieldCheckmarkOutline, SaveOutline, SearchOutline, StopOutline, ConstructOutline,
  CloudUploadOutline, PersonOutline,
} from '@vicons/ionicons5'

function icon(component: Component) {
  return defineComponent({ setup: () => () => h('span', { class: 'eic-icon', 'aria-hidden': true }, [h(component)]) })
}
export const ApartmentOutlined=icon(GitNetworkOutline), BarsOutlined=icon(ListOutline), BellOutlined=icon(NotificationsOutline)
export const BookOutlined=icon(BookOutline), CheckCircleOutlined=icon(CheckmarkCircleOutline), CloseCircleOutlined=icon(CloseCircleOutline)
export const CloseOutlined=icon(CloseOutline), CodeOutlined=icon(CodeSlashOutline), DatabaseOutlined=icon(ServerOutline)
export const DeleteOutlined=icon(TrashOutline), DownloadOutlined=icon(DownloadOutline), EditOutlined=icon(CreateOutline)
export const EllipsisOutlined=icon(EllipsisHorizontalOutline), ExperimentOutlined=icon(FlaskOutline), EyeOutlined=icon(EyeOutline)
export const FileOutlined=icon(DocumentOutline), FileTextOutlined=icon(DocumentTextOutline), FolderOpenOutlined=icon(FolderOpenOutline)
export const FolderOutlined=icon(FolderOutline), HistoryOutlined=icon(TimeOutline), HomeOutlined=icon(HomeOutline)
export const ImportOutlined=icon(EnterOutline), KeyOutlined=icon(KeyOutline), MailOutlined=icon(MailOutline)
export const MessageOutlined=icon(ChatbubblesOutline), PaperClipOutlined=icon(AttachOutline), PauseCircleOutlined=icon(PauseCircleOutline)
export const PlayCircleOutlined=icon(PlayCircleOutline), PlusOutlined=icon(AddOutline), ReloadOutlined=icon(ReloadOutline)
export const RollbackOutlined=icon(ReturnUpBackOutline), SafetyCertificateOutlined=icon(ShieldCheckmarkOutline), SaveOutlined=icon(SaveOutline)
export const SearchOutlined=icon(SearchOutline), StopOutlined=icon(StopOutline), ToolOutlined=icon(ConstructOutline)
export const UploadOutlined=icon(CloudUploadOutline), UserOutlined=icon(PersonOutline)
